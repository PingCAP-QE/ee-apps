from __future__ import annotations

import calendar
import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.api.queries.base import CommonFilters, bucket_expr, rate_pct, to_number

COST_STACK_LIMIT = 8
COST_STACK_OTHERS_DIMENSION = "__cost_stack_others__"
VALID_COST_STACK_GROUPS = frozenset(
    {
        "repo",
        "author",
        "owner",
        "team",
        "target_branch",
        "service",
        "sku",
        "cost_driver",
        "project",
        "region",
        "service_exec_id",
    }
)
COST_SHARE_LIMIT = 8
VALID_COST_SHARE_DIMENSIONS = frozenset(
    {"owner", "team", "service", "sku", "cost_driver", "project", "service_exec_id", "region"}
)
SOURCE_COST_DIMENSIONS = frozenset(
    {"service", "sku", "cost_driver", "project", "service_exec_id", "region"}
)
COST_DRILLDOWN_CHILD_GROUPS = {
    "team": "owner",
    "cost_driver": "sku",
}
LOW_REGION_SHARE_THRESHOLD_PCT = 1.0
UNMATCHED_RESOURCE_LIMIT = 10
UNMATCHED_RESOURCE_MAX_WINDOW_DAYS = 31
UNMATCHED_RESOURCE_SORTS = frozenset({"list_cost", "duration"})
NO_OWNER_LABEL = "(no owner)"
KUBERNETES_UNALLOCATED_RECORD_LIMIT = 100
ENGINEERING_GROUP_NAME = "Engineering Group"
COST_DATA_LAG_DAYS = 4
FORECAST_WINDOW_DAYS = 14
BUDGET_FALLBACK_MAX_DAYS = 31
CURRENT_ATTRIBUTION_BASIS = "current_attribution"
RESIDUAL_ALLOCATED_BASIS = "residual_allocated"
EQ_ALLOCATED_BASIS = "eq_allocated"
RESIDUAL_EQ_ALLOCATED_BASIS = "residual_eq_allocated"
MATERIALIZED_BASIS_KEYS = {
    RESIDUAL_ALLOCATED_BASIS: "kubernetes_allocated",
    EQ_ALLOCATED_BASIS: "eq_allocated",
    RESIDUAL_EQ_ALLOCATED_BASIS: "kubernetes_eq_allocated",
}
RESOURCE_SERVING_BASIS_KEYS = {
    CURRENT_ATTRIBUTION_BASIS: "native",
    **MATERIALIZED_BASIS_KEYS,
}
VALID_COST_ALLOCATION_BASES = frozenset(
    {CURRENT_ATTRIBUTION_BASIS, *MATERIALIZED_BASIS_KEYS}
)
COST_ATTRIBUTION_SOURCE_DATE_INDEX = "idx_cost_attribution_source_date_employee"
TIFLASH_COST_SOURCES = frozenset(
    {
        ("gcp", "pingcap-testing-account"),
        ("aws", "946646677266"),
    }
)
COST_KUBERNETES_ALLOCATION_SOURCE_DATE_INDEX = "idx_cost_kubernetes_allocation_source_date"
COST_UNMATCHED_SOURCE_DATE_NAMESPACE_INDEX = "idx_cost_unmatched_source_date_namespace"
COST_DRIVER_LABELS = {
    "compute": "Compute",
    "block_storage": "Block storage",
    "nat": "NAT",
    "data_transfer": "Data transfer",
    "object_storage": "Object storage",
    "logs": "Logs",
    "other": "Other",
}


@dataclass(frozen=True)
class BudgetPeriod:
    amount: float
    start_date: date
    end_date: date

    @property
    def days(self) -> int:
        return max((self.end_date - self.start_date).days + 1, 1)


@dataclass(frozen=True)
class CostAllocationBasis:
    """The cost rows and effective allocation basis for a dashboard request."""

    name: str
    from_clause: str = "cost_attribution_daily c"
    cte: str = ""
    preserves_source_dimensions: bool = False


def get_cost_page(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    cost_filters = _cost_filters(filters)
    if engine.dialect.name == "sqlite":
        sections = {
            "cost_trend": get_cost_trend(engine, cost_filters),
            "repo_group_stack": get_repo_group_cost_stack(engine, cost_filters),
            "engineering_group_share": get_engineering_group_share(engine, cost_filters),
        }
    else:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                "cost_trend": executor.submit(get_cost_trend, engine, cost_filters),
                "repo_group_stack": executor.submit(
                    get_repo_group_cost_stack,
                    engine,
                    cost_filters,
                ),
                "engineering_group_share": executor.submit(
                    get_engineering_group_share,
                    engine,
                    cost_filters,
                ),
            }
            sections = {name: future.result() for name, future in futures.items()}

    return {
        "scope": cost_filters.meta(),
        **sections,
    }


def get_cost_trend(
    engine: Engine,
    filters: CommonFilters,
    *,
    drilldown_group: str | None = None,
    drilldown_value: str | None = None,
    allocation_basis: str = CURRENT_ATTRIBUTION_BASIS,
) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        basis = _cost_allocation_basis(connection, filters, allocation_basis)
        drilldown = _cost_drilldown_filter(
            connection,
            child_group=None,
            drilldown_group=drilldown_group,
            drilldown_value=drilldown_value,
        )
        query_basis = _cost_basis_for_dimension(
            basis,
            drilldown["group"] if drilldown else None,
        )
        from_clause = query_basis.from_clause
        index_hint = _cost_basis_index_hint(connection, filters, query_basis)
        if drilldown:
            from_clause = _cost_basis_from_clause(query_basis, drilldown["from_clause"])
            where_clause = f"{where_clause} AND {drilldown['condition']}"
            params = {**params, **drilldown["params"]}
        bucket = bucket_expr(connection, "c.usage_date", filters.granularity)
        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
                {query_basis.cte}
                SELECT {index_hint}
                  {bucket} AS bucket_start,
                  SUM(c.net_cost) AS net_cost,
                  SUM(c.effective_cost) AS effective_cost,
                  SUM({list_cost_expr}) AS list_cost
                FROM {from_clause}
                WHERE {where_clause}
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        buckets = _bucket_starts(filters, data_rows)
        budget_targets = _budget_targets_for_filters(
            connection,
            filters,
            buckets=buckets,
        )
        coverage_row = connection.execute(
            text(
                f"""
                {query_basis.cte}
                SELECT {index_hint}
                  SUM({list_cost_expr}) AS total_resource_cost,
                  SUM(CASE WHEN c.attribution_status = 'matched' THEN {list_cost_expr} ELSE 0 END) AS matched_resource_cost
                FROM {from_clause}
                WHERE {where_clause}
                  AND c.list_cost IS NOT NULL
                """
            ),
            params,
        ).mappings().first()

    summary_net_cost = sum(_money(row["net_cost"]) for row in data_rows)
    summary_effective_cost = sum(_money(row["effective_cost"]) for row in data_rows)
    summary_list_cost = sum(_money(row["list_cost"]) for row in data_rows)
    total_resource_cost = _money(coverage_row["total_resource_cost"]) if coverage_row else 0.0
    matched_resource_cost = _money(coverage_row["matched_resource_cost"]) if coverage_row else 0.0

    net_cost_by_bucket = {bucket: 0.0 for bucket in buckets}
    list_cost_by_bucket = {bucket: 0.0 for bucket in buckets}
    for row in data_rows:
        bucket_start = str(row["bucket_start"])
        net_cost_by_bucket[bucket_start] = _money(row["net_cost"])
        list_cost_by_bucket[bucket_start] = _money(row["list_cost"])

    return {
        "series": [
            {
                "key": "list_cost",
                "label": "List cost",
                "type": "bar",
                "points": [[bucket, list_cost_by_bucket[bucket]] for bucket in buckets],
            },
            {
                "key": "net_cost",
                "label": "Net cost",
                "type": "line",
                "points": [[bucket, net_cost_by_bucket[bucket]] for bucket in buckets],
            },
        ],
        "meta": {
            **filters.meta(),
            **(
                {
                    "drilldown_group": drilldown["group"],
                    "drilldown_value": drilldown["value"],
                }
                if drilldown
                else {}
            ),
            "budget_targets": budget_targets,
            "allocation_basis": basis.name,
            "summary": {
                "net_cost": round(summary_net_cost, 2),
                "effective_cost": round(summary_effective_cost, 2),
                "list_cost": round(summary_list_cost, 2),
                "matched_resource_pct": rate_pct(matched_resource_cost, total_resource_cost),
                "matched_resource_cost": matched_resource_cost,
                "total_resource_cost": total_resource_cost,
            },
        },
    }


def get_cost_allocation_overview(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    """Return Kubernetes allocated and unallocated metrics alongside cost breakdowns."""
    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        attr_index_hint = _cost_attribution_index_hint(connection, filters)
        allocation_index_hint = _cost_kubernetes_allocation_read_hint(connection, filters)
        list_cost_expr = _billing_report_list_cost_expr("c")
        workload_split_condition = """
            (
              c.source_allocation_scope IN (
                'kubernetes_pod',
                'eks_pod',
                'gke_pod',
                'gke_direct',
                'tke_pod'
              )
              OR (
                c.source_allocation_scope = 'split_child'
                AND NULLIF(c.namespace, '') IS NOT NULL
              )
            )
        """
        kubernetes_unallocated_condition = _kubernetes_unallocated_condition(connection, "c")
        kubernetes_parent_residual_condition = _kubernetes_parent_residual_condition("c")
        legacy_person_attribution_condition = _has_valid_legacy_person_attribution("c")
        if _cost_kubernetes_allocation_table_exists(connection):
            allocation_where_clause, allocation_params = _build_cost_where(filters, table_alias="a")
            allocation_date_filters = replace(filters, branch=None)
            allocation_date_where_clause, allocation_date_params = _build_cost_where(
                allocation_date_filters,
                table_alias="a",
            )
            allocation_rows_cte = f"""
                WITH {_kubernetes_allocation_fact_active_roster_cte()},
                allocation_fact AS (
                  SELECT {allocation_index_hint}
                    CASE
                      WHEN {_kubernetes_allocation_fact_allocated_condition('a', 'roster')}
                        THEN 'workload_split'
                      ELSE 'unallocated'
                    END AS allocation_scope,
                    a.list_cost
                  FROM cost_kubernetes_workload_allocation_daily a
                  {_kubernetes_allocation_fact_roster_join('a', 'roster')}
                  WHERE {allocation_where_clause}
                    AND (
                      {_kubernetes_allocation_fact_allocated_condition('a', 'roster')}
                      OR {_kubernetes_allocation_fact_unallocated_condition('a', 'roster')}
                    )
                ), allocation_fact_dates AS (
                  SELECT {allocation_index_hint} DISTINCT
                    a.vendor,
                    a.account_id,
                    a.usage_date
                  FROM cost_kubernetes_workload_allocation_daily a
                  WHERE {allocation_date_where_clause}
                ), legacy_rows AS (
                  SELECT {attr_index_hint}
                    CASE
                      WHEN (
                        {workload_split_condition}
                        OR (
                          {kubernetes_parent_residual_condition}
                          AND {legacy_person_attribution_condition}
                        )
                      ) THEN 'workload_split'
                      ELSE 'unallocated'
                    END AS allocation_scope,
                    {list_cost_expr} AS list_cost
                  FROM cost_attribution_daily c
                  WHERE {where_clause}
                    AND (
                      {workload_split_condition}
                      OR (
                        {kubernetes_parent_residual_condition}
                        AND {legacy_person_attribution_condition}
                      )
                      OR {kubernetes_unallocated_condition}
                    )
                    -- An allocation fact is authoritative for its source/date. This
                    -- prevents node or control-plane costs from being counted twice.
                    AND NOT EXISTS (
                      SELECT 1
                      FROM allocation_fact_dates a
                      WHERE a.vendor = c.vendor
                        AND a.account_id = c.account_id
                        AND a.usage_date = c.usage_date
                    )
                ), allocation_rows AS (
                  SELECT allocation_scope, list_cost FROM allocation_fact
                  UNION ALL
                  SELECT allocation_scope, list_cost FROM legacy_rows
                )
            """
            query_params = {**params, **allocation_params, **allocation_date_params}
        else:
            # Keep the page available while the schema migration is rolled out.
            allocation_rows_cte = f"""
                WITH allocation_rows AS (
                  SELECT {attr_index_hint}
                    CASE
                      WHEN (
                        {workload_split_condition}
                        OR (
                          {kubernetes_parent_residual_condition}
                          AND {legacy_person_attribution_condition}
                        )
                      ) THEN 'workload_split'
                      ELSE 'unallocated'
                    END AS allocation_scope,
                    {list_cost_expr} AS list_cost
                  FROM cost_attribution_daily c
                  WHERE {where_clause}
                    AND (
                      {workload_split_condition}
                      OR (
                        {kubernetes_parent_residual_condition}
                        AND {legacy_person_attribution_condition}
                      )
                      OR {kubernetes_unallocated_condition}
                    )
                )
            """
            query_params = params
        row = connection.execute(
            text(
                f"""
                {allocation_rows_cte}
                SELECT
                  SUM(
                    CASE WHEN allocation_scope = 'workload_split'
                      THEN list_cost
                      ELSE 0
                    END
                  ) AS workload_split_cost,
                  SUM(
                    CASE WHEN allocation_scope = 'unallocated'
                      THEN list_cost
                      ELSE 0
                    END
                  ) AS kubernetes_unallocated_cost,
                  COUNT(*) AS allocation_cost_row_count
                FROM allocation_rows
                """
            ),
            query_params,
        ).mappings().first()

    workload_split_cost = _money(row["workload_split_cost"]) if row else 0.0
    kubernetes_unallocated_cost = _money(row["kubernetes_unallocated_cost"]) if row else 0.0
    allocation_cost_row_count = int(row["allocation_cost_row_count"] or 0) if row else 0
    return {
        "scope": filters.meta(),
        "is_available": allocation_cost_row_count > 0,
        "workload_split_cost": workload_split_cost,
        "kubernetes_unallocated_cost": kubernetes_unallocated_cost,
    }


def get_kubernetes_unallocated_costs(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    """Return Kubernetes costs without a valid person allocation by service and region."""
    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        attr_index_hint = _cost_attribution_index_hint(connection, filters)
        allocation_index_hint = _cost_kubernetes_allocation_read_hint(connection, filters)
        list_cost_expr = _billing_report_list_cost_expr("c")
        kubernetes_unallocated_condition = _kubernetes_unallocated_condition(
            connection,
            "c",
        )
        if _cost_kubernetes_allocation_table_exists(connection):
            fact_where_clause, fact_params = _build_cost_where(filters, table_alias="a")
            date_filters = replace(filters, branch=None)
            fact_date_where_clause, fact_date_params = _build_cost_where(
                date_filters,
                table_alias="a",
            )
            fact_service_expr = _kubernetes_allocation_fact_service_expr("a")
            allocation_fact_cte = f"""
                WITH {_kubernetes_allocation_fact_active_roster_cte()},
                allocation_fact_dates AS (
                  SELECT {allocation_index_hint} DISTINCT a.vendor, a.account_id, a.usage_date
                  FROM cost_kubernetes_workload_allocation_daily a
                  WHERE {fact_date_where_clause}
                ), allocation_fact_rows AS (
                  SELECT {allocation_index_hint}
                    -- Use the provider's billing-service name when an allocation
                    -- fact writer defines one; unknown vendors remain generic.
                    {fact_service_expr} AS service_name,
                    COALESCE(NULLIF(a.cluster_location, ''), '(no region)') AS region,
                    SUM(a.list_cost) AS list_cost,
                    CAST(NULL AS DECIMAL(16, 2)) AS effective_cost,
                    CAST(NULL AS DECIMAL(16, 2)) AS net_cost,
                    COUNT(*) AS cost_record_count
                  FROM cost_kubernetes_workload_allocation_daily a
                  {_kubernetes_allocation_fact_roster_join('a', 'roster')}
                  WHERE {fact_where_clause}
                    AND {_kubernetes_allocation_fact_unallocated_condition('a', 'roster')}
                  GROUP BY service_name, region
                ),
            """
            legacy_exclusion = """
                    AND NOT EXISTS (
                      SELECT 1
                      FROM allocation_fact_dates a
                      WHERE a.vendor = c.vendor
                        AND a.account_id = c.account_id
                        AND a.usage_date = c.usage_date
                    )
            """
            query_params = {**params, **fact_params, **fact_date_params}
            all_rows_prefix = "SELECT * FROM allocation_fact_rows UNION ALL"
        else:
            allocation_fact_cte = "WITH"
            legacy_exclusion = ""
            query_params = params
            all_rows_prefix = ""
        rows = connection.execute(
            text(
                f"""
                {allocation_fact_cte} legacy_rows AS (
                  SELECT {attr_index_hint}
                    COALESCE(NULLIF(c.service_name, ''), '(no service)') AS service_name,
                    COALESCE(NULLIF(c.region, ''), '(no region)') AS region,
                    SUM({list_cost_expr}) AS list_cost,
                    SUM(c.effective_cost) AS effective_cost,
                    SUM(c.net_cost) AS net_cost,
                    COUNT(*) AS cost_record_count
                  FROM cost_attribution_daily c
                  WHERE {where_clause}
                    AND {kubernetes_unallocated_condition}
                    {legacy_exclusion}
                  GROUP BY service_name, region
                ), all_rows AS (
                  {all_rows_prefix}
                  SELECT * FROM legacy_rows
                )
                SELECT
                  service_name,
                  region,
                  SUM(list_cost) AS list_cost,
                  SUM(effective_cost) AS effective_cost,
                  SUM(net_cost) AS net_cost,
                  SUM(cost_record_count) AS cost_record_count
                FROM all_rows
                GROUP BY service_name, region
                ORDER BY list_cost DESC, effective_cost DESC, service_name, region
                """
            ),
            query_params,
        ).mappings()

    items = [
        {
            "service_name": str(row["service_name"]),
            "region": str(row["region"]),
            "list_cost": _money(row["list_cost"]),
            "effective_cost": (
                None if row["effective_cost"] is None else _money(row["effective_cost"])
            ),
            "net_cost": None if row["net_cost"] is None else _money(row["net_cost"]),
            "cost_record_count": int(row["cost_record_count"] or 0),
        }
        for row in rows
    ]
    return {
        "scope": filters.meta(),
        "is_available": bool(items),
        "items": items,
    }


def get_kubernetes_unallocated_records(
    engine: Engine,
    filters: CommonFilters,
    *,
    service_name: str,
    region: str,
    limit: int = KUBERNETES_UNALLOCATED_RECORD_LIMIT,
) -> dict[str, Any]:
    """Return user-facing cost groups behind one Kubernetes service/region summary."""
    service_name = service_name.strip()
    region = region.strip()
    limit = max(1, min(limit, KUBERNETES_UNALLOCATED_RECORD_LIMIT))

    with engine.begin() as connection:
        legacy_where_clause, legacy_params = _build_cost_where(filters, table_alias="c")
        attr_index_hint = _cost_attribution_index_hint(connection, filters)
        allocation_index_hint = _cost_kubernetes_allocation_read_hint(connection, filters)
        record_params = {
            **legacy_params,
            "record_service_name": service_name,
            "record_region": region,
        }
        record_selects: list[str] = []
        cte_prefix = ""
        legacy_exclusion = ""

        if _cost_kubernetes_allocation_table_exists(connection):
            fact_where_clause, fact_params = _build_cost_where(filters, table_alias="a")
            date_filters = replace(filters, branch=None)
            fact_date_where_clause, fact_date_params = _build_cost_where(
                date_filters,
                table_alias="a",
            )
            fact_service_expr = _kubernetes_allocation_fact_service_expr("a")
            fact_region_expr = "COALESCE(NULLIF(a.cluster_location, ''), '(no region)')"
            record_selects.append(
                f"""
                SELECT {allocation_index_hint}
                  {fact_service_expr} AS service_name,
                  {fact_region_expr} AS region,
                  NULLIF(a.author, '') AS owner,
                  NULLIF(a.org, '') AS project,
                  NULLIF(a.repo, '') AS repo,
                  COALESCE(NULLIF(a.workload_name, ''), '') AS resource_name,
                  NULLIF(a.namespace, '') AS namespace,
                  '' AS labels,
                  a.list_cost AS list_cost
                FROM cost_kubernetes_workload_allocation_daily a
                {_kubernetes_allocation_fact_roster_join('a', 'roster')}
                WHERE {fact_where_clause}
                  AND {_kubernetes_allocation_fact_unallocated_condition('a', 'roster')}
                  AND {fact_service_expr} = :record_service_name
                  AND {fact_region_expr} = :record_region
                """
            )
            cte_prefix = f"""
                WITH {_kubernetes_allocation_fact_active_roster_cte()},
                allocation_fact_dates AS (
                  SELECT {allocation_index_hint} DISTINCT a.vendor, a.account_id, a.usage_date
                  FROM cost_kubernetes_workload_allocation_daily a
                  WHERE {fact_date_where_clause}
                ), records AS (
            """
            legacy_exclusion = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM allocation_fact_dates a
                    WHERE a.vendor = c.vendor
                      AND a.account_id = c.account_id
                      AND a.usage_date = c.usage_date
                  )
            """
            record_params.update(fact_params)
            record_params.update(fact_date_params)

        legacy_service_expr = "COALESCE(NULLIF(c.service_name, ''), '(no service)')"
        legacy_region_expr = "COALESCE(NULLIF(c.region, ''), '(no region)')"
        record_selects.append(
            f"""
            SELECT {attr_index_hint}
              {legacy_service_expr} AS service_name,
              {legacy_region_expr} AS region,
              NULLIF(c.owner, '') AS owner,
              NULLIF(c.project, '') AS project,
              NULLIF(c.repo, '') AS repo,
              NULLIF(c.resource_name, '') AS resource_name,
              NULLIF(c.namespace, '') AS namespace,
              {_json_text_expr(connection, 'c.vendor_tags_json')} AS labels,
              {_billing_report_list_cost_expr('c')} AS list_cost
            FROM cost_attribution_daily c
            WHERE {legacy_where_clause}
              AND {_kubernetes_unallocated_condition(connection, 'c')}
              AND {legacy_service_expr} = :record_service_name
              AND {legacy_region_expr} = :record_region
              {legacy_exclusion}
            """
        )

        if cte_prefix:
            records_sql = f"{cte_prefix}{' UNION ALL '.join(record_selects)}),"
        else:
            records_sql = f"WITH records AS ({record_selects[0]}),"

        rows = connection.execute(
            text(
                f"""
                {records_sql}
                grouped_records AS (
                  SELECT
                    COALESCE(owner, '') AS owner,
                    COALESCE(project, '') AS project,
                    COALESCE(repo, '') AS repo,
                    COALESCE(resource_name, '') AS resource_name,
                    COALESCE(namespace, '') AS namespace,
                    COALESCE(labels, '') AS labels,
                    COUNT(*) AS cost_record_count,
                    SUM(list_cost) AS list_cost
                  FROM records
                  GROUP BY
                    COALESCE(owner, ''),
                    COALESCE(project, ''),
                    COALESCE(repo, ''),
                    COALESCE(resource_name, ''),
                    COALESCE(namespace, ''),
                    COALESCE(labels, '')
                )
                SELECT
                  owner,
                  project,
                  repo,
                  resource_name,
                  namespace,
                  labels,
                  cost_record_count,
                  list_cost
                FROM grouped_records
                """
            ),
            record_params,
        ).mappings()

    # Normalize JSON key ordering before grouping so equivalent vendor-label
    # objects from different source rows remain a single visible cost group.
    grouped_items: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        labels = _format_vendor_labels(row["labels"])
        key = (
            str(row["owner"] or ""),
            str(row["project"] or ""),
            str(row["repo"] or ""),
            _user_facing_dimension(row["resource_name"]),
            _user_facing_dimension(row["namespace"]),
            labels,
        )
        item = grouped_items.setdefault(
            key,
            {
                "owner": key[0],
                "project": key[1],
                "repo": key[2],
                "resource_name": key[3],
                "namespace": key[4],
                "labels": key[5],
                "cost_record_count": 0,
                "list_cost": 0.0,
            },
        )
        item["cost_record_count"] += int(row["cost_record_count"] or 0)
        item["list_cost"] += _money(row["list_cost"])

    all_items = sorted(
        grouped_items.values(),
        key=lambda item: (
            -item["list_cost"],
            item["owner"],
            item["project"],
            item["repo"],
            item["resource_name"],
            item["namespace"],
            item["labels"],
        ),
    )
    total_count = len(all_items)
    items = [
        {**item, "list_cost": _money(item["list_cost"])}
        for item in all_items[:limit]
    ]
    return {
        "scope": filters.meta(),
        "service_name": service_name,
        "region": region,
        "total_count": total_count,
        "returned_count": len(items),
        "has_more": total_count > len(items),
        "items": items,
    }


def get_weekly_overview(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    cost_filters = _cost_filters(filters)
    previous_start, previous_end = _previous_window(cost_filters)
    previous_filters = CommonFilters(
        start_date=previous_start,
        end_date=previous_end,
        branch=cost_filters.branch,
        granularity=cost_filters.granularity,
        cost_vendor=cost_filters.cost_vendor,
        cost_account_id=cost_filters.cost_account_id,
    )
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            current_summary = _cost_summary(connection, cost_filters)
            previous_summary = _cost_summary(connection, previous_filters)
            budget_health = _budget_health_snapshot(connection, cost_filters)
            service_share = _service_share_by_threshold(
                connection,
                cost_filters,
                min_share_pct=1.0,
            )
            level2_share = _engineering_share_by_level_threshold(
                connection,
                cost_filters,
                level=2,
                min_share_pct=1.0,
            )
    else:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                "current_summary": executor.submit(_get_cost_summary, engine, cost_filters),
                "previous_summary": executor.submit(_get_cost_summary, engine, previous_filters),
                "budget_health": executor.submit(_get_budget_health_snapshot, engine, cost_filters),
                "service_share": executor.submit(
                    _get_service_share_by_threshold,
                    engine,
                    cost_filters,
                    min_share_pct=1.0,
                ),
                "level2_share": executor.submit(
                    _get_engineering_share_by_level_threshold,
                    engine,
                    cost_filters,
                    level=2,
                    min_share_pct=1.0,
                ),
            }
            sections = {name: future.result() for name, future in futures.items()}
        current_summary = sections["current_summary"]
        previous_summary = sections["previous_summary"]
        budget_health = sections["budget_health"]
        service_share = sections["service_share"]
        level2_share = sections["level2_share"]

    return {
        "scope": cost_filters.meta(),
        "previous_scope": previous_filters.meta(),
        "summary": {
            "list_cost": current_summary["list_cost"],
            "net_cost": current_summary["net_cost"],
            "previous_list_cost": previous_summary["list_cost"],
            "previous_net_cost": previous_summary["net_cost"],
            "list_cost_wow_pct": rate_pct(
                current_summary["list_cost"] - previous_summary["list_cost"],
                previous_summary["list_cost"],
            ),
            "net_cost_wow_pct": rate_pct(
                current_summary["net_cost"] - previous_summary["net_cost"],
                previous_summary["net_cost"],
            ),
        },
        "budget_health": budget_health,
        "service_share": service_share,
        "level2_share": level2_share,
    }


def _get_cost_summary(engine: Engine, filters: CommonFilters) -> dict[str, float]:
    with engine.begin() as connection:
        return _cost_summary(connection, filters)


def _get_service_share_by_threshold(
    engine: Engine,
    filters: CommonFilters,
    *,
    min_share_pct: float,
) -> dict[str, Any]:
    with engine.begin() as connection:
        return _service_share_by_threshold(connection, filters, min_share_pct=min_share_pct)


def _get_budget_health_snapshot(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any] | None:
    with engine.begin() as connection:
        return _budget_health_snapshot(connection, filters)


def _get_engineering_share_by_level_threshold(
    engine: Engine,
    filters: CommonFilters,
    *,
    level: int,
    min_share_pct: float,
) -> dict[str, Any]:
    with engine.begin() as connection:
        return _engineering_share_by_level_threshold(
            connection,
            filters,
            level=level,
            min_share_pct=min_share_pct,
        )


def get_repo_group_cost_stack(
    engine: Engine,
    filters: CommonFilters,
    *,
    group_by: str = "repo",
    drilldown_group: str | None = None,
    drilldown_value: str | None = None,
    allocation_basis: str = CURRENT_ATTRIBUTION_BASIS,
) -> dict[str, Any]:
    if group_by not in VALID_COST_STACK_GROUPS:
        group_by = "repo"

    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        basis = _cost_allocation_basis(connection, filters, allocation_basis)
        query_basis = _cost_basis_for_dimension(basis, group_by)
        index_hint = _cost_basis_index_hint(connection, filters, query_basis)
        bucket = bucket_expr(connection, "c.usage_date", filters.granularity)
        dimension = _cost_stack_dimension(connection, group_by)
        drilldown = _cost_drilldown_filter(
            connection,
            child_group=group_by,
            drilldown_group=drilldown_group,
            drilldown_value=drilldown_value,
        )
        if drilldown:
            dimension = {
                **dimension,
                "from_clause": drilldown["from_clause"],
                "params": {**dimension["params"], **drilldown["params"]},
            }
            where_clause = f"{where_clause} AND {drilldown['condition']}"
        dimension = _cost_basis_dimension(query_basis, dimension)
        list_cost_expr = _billing_report_list_cost_expr("c")
        top_rows = connection.execute(
            text(
                f"""
                {query_basis.cte}
                SELECT {index_hint}
                  {dimension["expr"]} AS dimension_name,
                  SUM({list_cost_expr}) AS list_cost
                FROM {dimension["from_clause"]}
                WHERE {where_clause}
                GROUP BY dimension_name
                ORDER BY list_cost DESC, dimension_name
                LIMIT :limit
                """
            ),
            {
                **params,
                **dimension["params"],
                # Fetch one additional dimension so we only create an Others series
                # when more than COST_STACK_LIMIT dimensions actually exist.
                "limit": COST_STACK_LIMIT + 1,
            },
        ).mappings()
        top_dimensions = [str(row["dimension_name"] or dimension["empty_label"]) for row in top_rows]
        if not top_dimensions:
            return {
                "series": [],
                "items": [],
                "meta": _cost_dimension_meta(
                    filters,
                    limit=COST_STACK_LIMIT,
                    dimension_key="group_by",
                    dimension=group_by,
                    drilldown=drilldown,
                    allocation_basis=basis.name,
                ),
            }

        has_others = len(top_dimensions) > COST_STACK_LIMIT
        visible_dimensions = (
            top_dimensions[: COST_STACK_LIMIT - 1] if has_others else top_dimensions
        )
        dimension_conditions = []
        dimension_params: dict[str, Any] = {}
        for index, dimension_name in enumerate(visible_dimensions):
            dimension_key = f"dimension_{index}"
            dimension_conditions.append(
                f"{dimension['expr']} = :{dimension_key}"
            )
            dimension_params[dimension_key] = dimension_name

        stack_dimension = dimension["expr"]
        if has_others:
            stack_dimension = (
                f"CASE WHEN {' OR '.join(dimension_conditions)} "
                f"THEN {dimension['expr']} ELSE :others_dimension END"
            )
            dimension_params["others_dimension"] = COST_STACK_OTHERS_DIMENSION

        rows = connection.execute(
            text(
                f"""
                {query_basis.cte}
                SELECT {index_hint}
                  {bucket} AS bucket_start,
                  {stack_dimension} AS dimension_name,
                  SUM({list_cost_expr}) AS list_cost
                FROM {dimension["from_clause"]}
                WHERE {where_clause}
                GROUP BY bucket_start, dimension_name
                ORDER BY bucket_start, dimension_name
                """
            ),
            {**params, **dimension["params"], **dimension_params},
        ).mappings()
        data_rows = [dict(row) for row in rows]

    buckets = _bucket_starts(filters, data_rows)
    stack_dimensions = [
        *visible_dimensions,
        *([COST_STACK_OTHERS_DIMENSION] if has_others else []),
    ]
    others_key = (
        _cost_stack_key(group_by, COST_STACK_OTHERS_DIMENSION, len(stack_dimensions) - 1)
        if has_others
        else None
    )
    values_by_key = {
        _cost_stack_key(group_by, dimension_name, index): {bucket: 0.0 for bucket in buckets}
        for index, dimension_name in enumerate(stack_dimensions)
    }
    labels_by_key = {
        _cost_stack_key(group_by, dimension_name, index): (
            "Others" if dimension_name == COST_STACK_OTHERS_DIMENSION else dimension_name
        )
        for index, dimension_name in enumerate(stack_dimensions)
    }
    key_by_name = {
        dimension_name: _cost_stack_key(group_by, dimension_name, index)
        for index, dimension_name in enumerate(stack_dimensions)
    }
    for row in data_rows:
        dimension_name = str(row["dimension_name"] or dimension["empty_label"])
        key = key_by_name[dimension_name]
        values_by_key[key][str(row["bucket_start"])] = _money(row["list_cost"])

    return {
        "series": [
            {
                "key": key,
                "label": labels_by_key[key],
                "type": "bar",
                "points": [[bucket, values_by_key[key].get(bucket, 0.0)] for bucket in buckets],
            }
            for key in values_by_key
        ],
        "items": [
            {
                "name": labels_by_key[key],
                "value": round(sum(values_by_key[key].values()), 2),
                **(
                    {"interactive": False}
                    if key == others_key
                    else {}
                ),
            }
            for key in values_by_key
        ],
        "meta": _cost_dimension_meta(
            filters,
            limit=COST_STACK_LIMIT,
            dimension_key="group_by",
            dimension=group_by,
            drilldown=drilldown,
            allocation_basis=basis.name,
        ),
    }


def get_cost_share(
    engine: Engine,
    filters: CommonFilters,
    *,
    dimension: str = "owner",
    drilldown_group: str | None = None,
    drilldown_value: str | None = None,
    allocation_basis: str = CURRENT_ATTRIBUTION_BASIS,
) -> dict[str, Any]:
    if dimension not in VALID_COST_SHARE_DIMENSIONS:
        dimension = "owner"

    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        basis = _cost_allocation_basis(connection, filters, allocation_basis)
        query_basis = _cost_basis_for_dimension(basis, dimension)
        index_hint = _cost_basis_index_hint(connection, filters, query_basis)
        dimension_config = _cost_share_dimension(connection, dimension)
        drilldown = _cost_drilldown_filter(
            connection,
            child_group=dimension,
            drilldown_group=drilldown_group,
            drilldown_value=drilldown_value,
        )
        if drilldown:
            dimension_config = {
                **dimension_config,
                "from_clause": drilldown["from_clause"],
                "params": {**dimension_config["params"], **drilldown["params"]},
            }
            where_clause = f"{where_clause} AND {drilldown['condition']}"
        dimension_config = _cost_basis_dimension(query_basis, dimension_config)
        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
                {query_basis.cte}
                SELECT {index_hint}
                  {dimension_config["expr"]} AS dimension_name,
                  SUM({list_cost_expr}) AS list_cost
                FROM {dimension_config["from_clause"]}
                WHERE {where_clause}
                  AND c.list_cost IS NOT NULL
                GROUP BY dimension_name
                ORDER BY list_cost DESC, dimension_name
                """
            ),
            {**params, **dimension_config["params"]},
        ).mappings()
        all_items = []
        for row in rows:
            value = _money(row["list_cost"])
            if value <= 0:
                continue
            all_items.append(
                {
                    "name": str(row["dimension_name"] or dimension_config["empty_label"]),
                    "value": value,
                }
            )

    total = sum(item["value"] for item in all_items)
    for item in all_items:
        item["share_pct"] = rate_pct(item["value"], total)
        item["interactive"] = False
        if dimension == "region" and 0 < item["share_pct"] < LOW_REGION_SHARE_THRESHOLD_PCT:
            item["highlight"] = True

    meta = _cost_dimension_meta(
        filters,
        limit=COST_SHARE_LIMIT,
        dimension_key="dimension",
        dimension=dimension,
        drilldown=drilldown,
        total_list_cost=round(total, 2),
        allocation_basis=basis.name,
    )
    if dimension == "region":
        meta["highlight_threshold_pct"] = LOW_REGION_SHARE_THRESHOLD_PCT

    return {
        "items": _share_items_limited_with_others(
            all_items,
            limit=COST_SHARE_LIMIT,
            total=total,
        ),
        "meta": meta,
    }


def get_engineering_group_share(
    engine: Engine,
    filters: CommonFilters,
    *,
    allocation_basis: str = CURRENT_ATTRIBUTION_BASIS,
) -> dict[str, Any]:
    with engine.begin() as connection:
        basis = _cost_allocation_basis(connection, filters, allocation_basis)
        root = connection.execute(
            text(
                """
                SELECT id, path
                FROM roster_groups
                WHERE name = :group_name
                  AND is_active = 1
                ORDER BY id
                LIMIT 1
                """
            ),
            {"group_name": ENGINEERING_GROUP_NAME},
        ).mappings().first()
        if root is None:
            return {
                "level1": {
                    "items": [],
                    "meta": {
                        **filters.meta(),
                        "group_name": ENGINEERING_GROUP_NAME,
                        "allocation_basis": basis.name,
                    },
                },
                "level2": {
                    "items": [],
                    "meta": {
                        **filters.meta(),
                        "group_name": ENGINEERING_GROUP_NAME,
                        "allocation_basis": basis.name,
                    },
                },
            }

        level1 = _engineering_share_by_level(connection, filters, root, level=1, basis=basis)
        level2 = _engineering_share_by_level(connection, filters, root, level=2, basis=basis)

    return {
        "level1": level1,
        "level2": level2,
    }


def list_cost_sources(engine: Engine) -> dict[str, Any]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT vendor, account_id, display_name
                FROM cost_sources
                WHERE is_active = :is_active
                ORDER BY vendor, account_id
                """
            ),
            {"is_active": 1},
        ).mappings()
        items = [
            {
                "value": _cost_source_value(str(row["vendor"]), str(row["account_id"])),
                "label": _cost_source_label(str(row["vendor"]), str(row["account_id"])),
                "vendor": str(row["vendor"]),
                "account_id": str(row["account_id"]),
                "display_name": str(row["display_name"] or ""),
            }
            for row in rows
        ]
    return {"items": items}


def get_weekly_account_summaries(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any]:
    cost_filters = _cost_filters(filters)
    previous_start, previous_end = _previous_window(cost_filters)
    if (
        cost_filters.start_date is None
        or cost_filters.end_date is None
        or previous_start is None
        or previous_end is None
    ):
        return {"scope": cost_filters.meta(), "items": []}

    branch_join_clause = "AND c.target_branch = :branch" if cost_filters.branch else ""
    params = {
        "current_start": cost_filters.start_date,
        "current_end": cost_filters.end_date,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "is_active": 1,
    }
    if cost_filters.branch:
        params["branch"] = cost_filters.branch

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT
                  s.vendor,
                  s.account_id,
                  s.display_name,
                  SUM(
                    CASE WHEN c.usage_date BETWEEN :current_start AND :current_end
                      THEN COALESCE(c.net_cost, 0) ELSE 0 END
                  ) AS net_cost,
                  SUM(
                    CASE WHEN c.usage_date BETWEEN :previous_start AND :previous_end
                      THEN COALESCE(c.net_cost, 0) ELSE 0 END
                  ) AS previous_net_cost
                FROM cost_sources s
                LEFT JOIN cost_attribution_daily c
                  ON c.vendor = s.vendor
                 AND c.account_id = s.account_id
                 AND c.usage_date BETWEEN :previous_start AND :current_end
                 {branch_join_clause}
                WHERE s.is_active = :is_active
                GROUP BY s.vendor, s.account_id, s.display_name
                ORDER BY s.vendor, s.account_id
                """
            ),
            params,
        ).mappings()
        budget_periods_by_account = _budget_periods_by_account_for_window(
            connection,
            start_date=cost_filters.start_date,
            end_date=cost_filters.end_date,
        )
        previous_budget_periods_by_account = _budget_periods_by_account_for_window(
            connection,
            start_date=cost_filters.end_date - timedelta(days=BUDGET_FALLBACK_MAX_DAYS),
            end_date=cost_filters.end_date - timedelta(days=1),
        )
        items = []
        for row in rows:
            vendor = str(row["vendor"])
            account_id = str(row["account_id"])
            budget_key = (vendor, account_id)
            budget_periods = budget_periods_by_account.get(budget_key, [])
            budget_period = _budget_period_for_date(budget_periods, cost_filters.end_date)
            if budget_period is None:
                budget_period = _recent_previous_budget_period(
                    previous_budget_periods_by_account.get(budget_key, []),
                    cost_filters.end_date,
                )
            annual_budget = budget_period.amount if budget_period else None
            net_cost = _money(row["net_cost"])
            previous_net_cost = _money(row["previous_net_cost"])
            weekly_budget = _budget_amount_for_periods(
                budget_periods,
                cost_filters.start_date,
                cost_filters.end_date,
            )
            if weekly_budget is None and budget_period:
                weekly_budget = _budget_amount_for_days(
                    budget_period,
                    (cost_filters.end_date - cost_filters.start_date).days + 1,
                )
            items.append(
                {
                    "cost_source": _cost_source_value(
                        vendor,
                        account_id,
                    ),
                    "vendor": vendor,
                    "account_id": account_id,
                    "display_name": str(row["display_name"] or ""),
                    "net_cost": net_cost,
                    "previous_net_cost": previous_net_cost,
                    "net_cost_wow_pct": rate_pct(
                        net_cost - previous_net_cost,
                        previous_net_cost,
                    ),
                    "annual_budget": annual_budget,
                    "period_budget": annual_budget,
                    "weekly_budget": weekly_budget,
                    "over_budget": weekly_budget is not None and net_cost > weekly_budget,
                }
            )

    return {
        "scope": cost_filters.meta(),
        "previous_scope": {
            **cost_filters.meta(),
            "start_date": previous_start.isoformat(),
            "end_date": previous_end.isoformat(),
        },
        "items": items,
    }


def get_unmatched_resources(
    engine: Engine,
    filters: CommonFilters,
    *,
    owner: str | None = None,
    service_name: str | None = None,
    sort_by: str = "list_cost",
    allocation_basis: str = CURRENT_ATTRIBUTION_BASIS,
) -> dict[str, Any]:
    return _get_published_unmatched_resources(
        engine,
        filters,
        owner=owner,
        service_name=service_name,
        sort_by=sort_by,
        allocation_basis=allocation_basis,
    )


def _get_published_unmatched_resources(
    engine: Engine,
    filters: CommonFilters,
    *,
    owner: str | None,
    service_name: str | None,
    sort_by: str,
    allocation_basis: str,
) -> dict[str, Any]:
    """Read only complete resource-serving publications for this request.

    Publication validity is checked before the Top-resource and service reads.
    A partial post-allocation rebuild is consequently a harmless 200/pending
    response rather than a partial result or the retired raw-ledger join.
    """
    requested_filters = filters
    if (
        filters.start_date is not None
        and filters.end_date is not None
        and (filters.end_date - filters.start_date).days + 1 > UNMATCHED_RESOURCE_MAX_WINDOW_DAYS
    ):
        filters = replace(
            filters,
            start_date=filters.end_date - timedelta(days=UNMATCHED_RESOURCE_MAX_WINDOW_DAYS - 1),
        )
    if sort_by not in UNMATCHED_RESOURCE_SORTS:
        sort_by = "list_cost"
    requested_basis = (
        allocation_basis if allocation_basis in RESOURCE_SERVING_BASIS_KEYS else CURRENT_ATTRIBUTION_BASIS
    )
    basis_key = RESOURCE_SERVING_BASIS_KEYS[requested_basis]
    selected_owner = owner or NO_OWNER_LABEL
    owner_value = "" if selected_owner == NO_OWNER_LABEL else selected_owner
    owner_key = hashlib.sha256(owner_value.encode("utf-8")).hexdigest()
    service_filter_name = service_name or None
    expected_dates = _resource_serving_dates(filters.start_date, filters.end_date)

    with engine.begin() as connection:
        source_available_column = (
            "source_available_from"
            if _table_has_column(connection, "cost_sources", "source_available_from")
            else "NULL"
        )
        sources = tuple(
            connection.execute(
                text(
                    f"""
                    SELECT vendor, account_id, {source_available_column} AS source_available_from
                    FROM cost_sources
                    WHERE is_active = 1
                      AND (:cost_vendor IS NULL OR vendor = :cost_vendor)
                      AND (:cost_account_id IS NULL OR account_id = :cost_account_id)
                    ORDER BY vendor, account_id
                    """
                ),
                {
                    "cost_vendor": filters.cost_vendor,
                    "cost_account_id": filters.cost_account_id,
                },
            ).mappings()
        )
        expected_windows = {
            (str(source["vendor"]), str(source["account_id"]), usage_date)
            for source in sources
            for usage_date in expected_dates
            if (
                _parse_date(source["source_available_from"]) is None
                or _parse_date(source["source_available_from"]) <= usage_date
            )
        }
        has_serving_tables = _table_exists(connection, "cost_resource_serving_daily") and _table_exists(
            connection, "cost_resource_serving_publication"
        )
        active_allocation_version = _resource_serving_active_allocation_version(connection)
        publication_rows: dict[tuple[str, str, date], Mapping[str, Any]] = {}
        if has_serving_tables and expected_dates:
            rows = connection.execute(
                text(
                    """
                    WITH scoped_sources AS (
                      SELECT vendor, account_id
                      FROM cost_sources
                      WHERE is_active = 1
                        AND (:cost_vendor IS NULL OR vendor = :cost_vendor)
                        AND (:cost_account_id IS NULL OR account_id = :cost_account_id)
                    )
                    SELECT p.vendor, p.account_id, p.usage_date,
                      p.source_allocation_version, p.source_row_count,
                      COUNT(s.id) AS serving_row_count
                    FROM cost_resource_serving_publication p
                    JOIN scoped_sources scope
                      ON scope.vendor = p.vendor AND scope.account_id = p.account_id
                    LEFT JOIN cost_resource_serving_daily s
                      ON s.basis_key = p.basis_key
                     AND s.vendor = p.vendor AND s.account_id = p.account_id
                     AND s.usage_date = p.usage_date
                     AND s.materialization_version = p.active_materialization_version
                    WHERE p.basis_key = :basis_key
                      AND p.usage_date BETWEEN :start_date AND :end_date
                    GROUP BY p.vendor, p.account_id, p.usage_date,
                      p.source_allocation_version, p.source_row_count
                    """
                ),
                {
                    "basis_key": basis_key,
                    "start_date": filters.start_date,
                    "end_date": filters.end_date,
                    "cost_vendor": filters.cost_vendor,
                    "cost_account_id": filters.cost_account_id,
                },
            ).mappings()
            publication_rows = {
                (str(row["vendor"]), str(row["account_id"]), _parse_date(row["usage_date"])): row
                for row in rows
                if _parse_date(row["usage_date"]) is not None
            }

        pending_dates = sorted(
            {
                usage_date.isoformat()
                for vendor, account_id, usage_date in expected_windows
                if not _resource_serving_window_is_valid(
                    publication_rows.get((vendor, account_id, usage_date)),
                    basis_key=basis_key,
                    active_allocation_version=active_allocation_version,
                )
            }
        )
        if pending_dates:
            return _resource_serving_response(
                items=[],
                filters=filters,
                requested_filters=requested_filters,
                selected_owner=selected_owner,
                service_name=service_filter_name,
                sort_by=sort_by,
                allocation_basis=requested_basis,
                services=[],
                pending_dates=pending_dates,
                detail_list_cost=0.0,
                total_list_cost=0.0,
                resource_data_source="attribution_fallback",
            )
        if not has_serving_tables:
            # This only occurs before migration while no active source/date is
            # expected. Never use the historical broad CTE as a compatibility path.
            return _resource_serving_response(
                items=[], filters=filters, requested_filters=requested_filters,
                selected_owner=selected_owner, service_name=service_filter_name, sort_by=sort_by,
                allocation_basis=requested_basis, services=[], pending_dates=[],
                detail_list_cost=0.0, total_list_cost=0.0,
                resource_data_source="attribution_fallback",
            )

        branch_clause = "AND s.target_branch = :branch" if filters.branch else ""
        params = {
            "basis_key": basis_key,
            "owner_key": owner_key,
            "start_date": filters.start_date,
            "end_date": filters.end_date,
            "cost_vendor": filters.cost_vendor,
            "cost_account_id": filters.cost_account_id,
            "service_name": service_filter_name,
            "active_allocation_version": active_allocation_version,
        }
        if filters.branch:
            params["branch"] = filters.branch
        validity_clause = "(s.basis_key = 'native' OR p.source_allocation_version = :active_allocation_version)"
        scoped_prefix = """
            WITH scoped_sources AS (
              SELECT vendor, account_id
              FROM cost_sources
              WHERE is_active = 1
                AND (:cost_vendor IS NULL OR vendor = :cost_vendor)
                AND (:cost_account_id IS NULL OR account_id = :cost_account_id)
            )
        """
        service_rows = connection.execute(
            text(
                f"""
                {scoped_prefix}
                SELECT DISTINCT COALESCE(NULLIF(s.service_name, ''), '(no service)') AS service_name
                FROM cost_resource_serving_daily s
                JOIN scoped_sources scope ON scope.vendor = s.vendor AND scope.account_id = s.account_id
                JOIN cost_resource_serving_publication p
                  ON p.basis_key = s.basis_key AND p.vendor = s.vendor AND p.account_id = s.account_id
                 AND p.usage_date = s.usage_date
                 AND p.active_materialization_version = s.materialization_version
                WHERE s.basis_key = :basis_key AND s.owner_key = :owner_key
                  AND s.usage_date BETWEEN :start_date AND :end_date
                  AND {validity_clause} {branch_clause}
                ORDER BY service_name
                """
            ),
            params,
        ).mappings()
        services = [
            {"value": str(row["service_name"]), "label": str(row["service_name"])}
            for row in service_rows
        ]
        order_by = (
            "usage_seconds DESC, list_cost DESC, resource_name"
            if sort_by == "duration"
            else "list_cost DESC, usage_seconds DESC, resource_name"
        )
        rows = connection.execute(
            text(
                f"""
                {scoped_prefix}
                SELECT
                  s.resource_group_key,
                  MIN(s.resource_name) AS resource_name,
                  GROUP_CONCAT(DISTINCT s.service_name) AS service_name,
                  MIN(s.representative_labels_json) AS representative_labels_json,
                  MIN(s.usage_date) AS first_seen_date,
                  MAX(s.usage_date) AS last_seen_date,
                  SUM(COALESCE(s.usage_seconds, 0)) AS usage_seconds,
                  SUM(s.list_cost) AS list_cost,
                  SUM(s.detail_list_cost) AS detail_list_cost,
                  SUM(s.fallback_list_cost) AS fallback_list_cost
                FROM cost_resource_serving_daily s
                JOIN scoped_sources scope ON scope.vendor = s.vendor AND scope.account_id = s.account_id
                JOIN cost_resource_serving_publication p
                  ON p.basis_key = s.basis_key AND p.vendor = s.vendor AND p.account_id = s.account_id
                 AND p.usage_date = s.usage_date
                 AND p.active_materialization_version = s.materialization_version
                WHERE s.basis_key = :basis_key AND s.owner_key = :owner_key
                  AND s.usage_date BETWEEN :start_date AND :end_date
                  AND (:service_name IS NULL OR s.service_name = :service_name)
                  AND {validity_clause} {branch_clause}
                GROUP BY s.resource_group_key
                ORDER BY {order_by}
                LIMIT :limit
                """
            ),
            {**params, "limit": UNMATCHED_RESOURCE_LIMIT},
        ).mappings()
        coverage = connection.execute(
            text(
                f"""
                {scoped_prefix}
                SELECT
                  COALESCE(SUM(s.detail_list_cost), 0) AS detail_list_cost,
                  COALESCE(SUM(s.fallback_list_cost), 0) AS fallback_list_cost,
                  COALESCE(SUM(s.list_cost), 0) AS total_list_cost
                FROM cost_resource_serving_daily s
                JOIN scoped_sources scope ON scope.vendor = s.vendor AND scope.account_id = s.account_id
                JOIN cost_resource_serving_publication p
                  ON p.basis_key = s.basis_key AND p.vendor = s.vendor AND p.account_id = s.account_id
                 AND p.usage_date = s.usage_date
                 AND p.active_materialization_version = s.materialization_version
                WHERE s.basis_key = :basis_key AND s.owner_key = :owner_key
                  AND s.usage_date BETWEEN :start_date AND :end_date
                  AND (:service_name IS NULL OR s.service_name = :service_name)
                  AND {validity_clause} {branch_clause}
                """
            ),
            params,
        ).mappings().one()
        detail_list_cost = Decimal(str(to_number(coverage["detail_list_cost"]) or 0))
        fallback_list_cost = Decimal(str(to_number(coverage["fallback_list_cost"]) or 0))
        total_list_cost = Decimal(str(to_number(coverage["total_list_cost"]) or 0))
        items = []
        for row in rows:
            detail = to_number(row["detail_list_cost"]) or 0
            fallback = to_number(row["fallback_list_cost"]) or 0
            items.append(
                {
                    "resource_name": str(row["resource_name"] or "(no resource name)"),
                    "service_name": str(row["service_name"] or ""),
                    "sku_name": "",
                    "repo_name": "",
                    "labels": _format_vendor_labels(row["representative_labels_json"]),
                    "allocation_buckets": "",
                    "first_seen_date": _date_text(row["first_seen_date"]),
                    "last_seen_date": _date_text(row["last_seen_date"]),
                    "observed_days": _observed_days(
                        row["first_seen_date"], row["last_seen_date"],
                        window_start=filters.start_date, window_end=filters.end_date,
                    ),
                    "attribution_source": "",
                    "attribution_status": "",
                    "usage_seconds": round(float(to_number(row["usage_seconds"]) or 0), 2),
                    "list_cost": _money(row["list_cost"]),
                    "resource_data_source": (
                        "mixed" if detail != 0 and fallback != 0 else
                        "resource_detail" if detail != 0 else "attribution_fallback"
                    ),
                    "resource_detail_cost": _money(detail),
                }
            )
    resource_data_source = "mixed" if detail_list_cost != 0 and fallback_list_cost != 0 else (
        "resource_detail" if detail_list_cost != 0 else "attribution_fallback"
    )
    return _resource_serving_response(
        items=items, filters=filters, requested_filters=requested_filters,
        selected_owner=selected_owner, service_name=service_filter_name, sort_by=sort_by,
        allocation_basis=requested_basis, services=services, pending_dates=[],
        detail_list_cost=float(detail_list_cost), total_list_cost=float(total_list_cost),
        resource_data_source=resource_data_source,
    )


def _resource_serving_dates(start_date: date | None, end_date: date | None) -> tuple[date, ...]:
    if start_date is None or end_date is None:
        return ()
    dates = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)
    return tuple(dates)


def _resource_serving_active_allocation_version(connection: Connection) -> str | None:
    if not _table_exists(connection, "cost_allocation_publication"):
        return None
    return connection.execute(
        text("SELECT active_allocation_version FROM cost_allocation_publication WHERE publication_name = 'dashboard'")
    ).scalar_one_or_none()


def _resource_serving_window_is_valid(
    row: Mapping[str, Any] | None,
    *,
    basis_key: str,
    active_allocation_version: str | None,
) -> bool:
    if row is None:
        return False
    if int(row["source_row_count"] or 0) > 0 and int(row["serving_row_count"] or 0) == 0:
        return False
    return basis_key == "native" or (
        active_allocation_version is not None
        and row["source_allocation_version"] == active_allocation_version
    )


def _resource_serving_response(
    *,
    items: list[dict[str, Any]],
    filters: CommonFilters,
    requested_filters: CommonFilters,
    selected_owner: str,
    service_name: str | None,
    sort_by: str,
    allocation_basis: str,
    services: list[dict[str, str]],
    pending_dates: list[str],
    detail_list_cost: float,
    total_list_cost: float,
    resource_data_source: str,
) -> dict[str, Any]:
    return {
        "items": items,
        "meta": {
            **filters.meta(),
            "requested_start_date": (
                requested_filters.start_date.isoformat() if requested_filters.start_date else None
            ),
            "window_limited": filters.start_date != requested_filters.start_date,
            "max_window_days": UNMATCHED_RESOURCE_MAX_WINDOW_DAYS,
            "limit": UNMATCHED_RESOURCE_LIMIT,
            "owner": selected_owner,
            "service_name": service_name,
            "sort_by": sort_by,
            "allocation_basis": allocation_basis,
            "resource_data_source": resource_data_source,
            "resource_detail_cost": _money(detail_list_cost),
            "resource_detail_coverage_pct": rate_pct(detail_list_cost, total_list_cost),
            "materialized": True,
            "pending_dates": pending_dates,
            "services": services,
        },
    }


def _engineering_share_by_level(
    connection: Connection,
    filters: CommonFilters,
    root: Any,
    *,
    level: int,
    basis: CostAllocationBasis | None = None,
) -> dict[str, Any]:
    basis = basis or CostAllocationBasis(CURRENT_ATTRIBUTION_BASIS)
    where_clause, params = _build_cost_where(filters, table_alias="c")
    index_hint = _cost_basis_index_hint(connection, filters, basis)
    like_expr = _like_prefix_expr(connection, "c_group.path", "target_group.path")
    list_cost_expr = _billing_report_list_cost_expr("c")
    if level == 1:
        hierarchy_joins = f"""
            JOIN roster_groups target_group
              ON target_group.is_active = 1
             AND target_group.parent_id = :root_id
             AND {like_expr}
        """
    else:
        hierarchy_joins = f"""
            JOIN roster_groups target_parent
              ON target_parent.is_active = 1
             AND target_parent.parent_id = :root_id
            JOIN roster_groups target_group
              ON target_group.is_active = 1
             AND target_group.parent_id = target_parent.id
             AND {like_expr}
        """
    rows = connection.execute(
        text(
            f"""
            {basis.cte}
            SELECT {index_hint}
              target_group.name AS group_name,
              SUM({list_cost_expr}) AS list_cost
            FROM {basis.from_clause}
            JOIN roster_groups c_group ON c_group.id = c.group_id
            {hierarchy_joins}
            WHERE {where_clause}
              AND c_group.path IS NOT NULL
              AND c_group.path LIKE :root_path_like
            GROUP BY target_group.id, target_group.name
            ORDER BY list_cost DESC, target_group.name
            """
        ),
        {
            **params,
            "root_id": root["id"],
            "root_path_like": f"{root['path']}%",
        },
    ).mappings()
    items = [
        {
            "name": str(row["group_name"]),
            "value": _money(row["list_cost"]),
        }
        for row in rows
    ]
    total = sum(item["value"] for item in items)
    for item in items:
        item["share_pct"] = rate_pct(item["value"], total)
        item["interactive"] = False

    return {
        "items": items,
        "meta": {
            **filters.meta(),
            "group_name": ENGINEERING_GROUP_NAME,
            "level": level,
            "total_list_cost": round(total, 2),
            "allocation_basis": basis.name,
        },
    }


def _engineering_share_by_level_threshold(
    connection: Connection,
    filters: CommonFilters,
    *,
    level: int,
    min_share_pct: float,
) -> dict[str, Any]:
    root = connection.execute(
        text(
            """
            SELECT id, path
            FROM roster_groups
            WHERE name = :group_name
              AND is_active = 1
            ORDER BY id
            LIMIT 1
            """
        ),
        {"group_name": ENGINEERING_GROUP_NAME},
    ).mappings().first()
    if root is None:
        return {
            "items": [],
            "meta": {
                **filters.meta(),
                "group_name": ENGINEERING_GROUP_NAME,
                "level": level,
                "min_share_pct": min_share_pct,
                "total_list_cost": 0.0,
            },
        }

    share = _engineering_share_by_level(connection, filters, root, level=level)
    return {
        "items": _share_items_above_threshold_with_others(
            share["items"],
            min_share_pct=min_share_pct,
            total=_number_or_zero(share["meta"].get("total_list_cost")),
        ),
        "meta": {
            **share["meta"],
            "min_share_pct": min_share_pct,
        },
    }


def _cost_summary(connection: Connection, filters: CommonFilters) -> dict[str, float]:
    where_clause, params = _build_cost_where(filters, table_alias="c")
    index_hint = _cost_aggregate_read_hint(connection, filters)
    list_cost_expr = _billing_report_list_cost_expr("c")
    row = connection.execute(
        text(
            f"""
            SELECT {index_hint}
              SUM({list_cost_expr}) AS list_cost,
              SUM(c.net_cost) AS net_cost
            FROM cost_attribution_daily c
            WHERE {where_clause}
            """
        ),
        params,
    ).mappings().first()
    return {
        "list_cost": _money(row["list_cost"]) if row else 0.0,
        "net_cost": _money(row["net_cost"]) if row else 0.0,
    }


def _service_share_by_threshold(
    connection: Connection,
    filters: CommonFilters,
    *,
    min_share_pct: float,
) -> dict[str, Any]:
    where_clause, params = _build_cost_where(filters, table_alias="c")
    index_hint = _cost_aggregate_read_hint(connection, filters)
    list_cost_expr = _billing_report_list_cost_expr("c")
    rows = connection.execute(
        text(
            f"""
            SELECT {index_hint}
              COALESCE(NULLIF(c.service_name, ''), '(no service)') AS service_name,
              SUM({list_cost_expr}) AS list_cost
            FROM cost_attribution_daily c
            WHERE {where_clause}
            GROUP BY service_name
            ORDER BY list_cost DESC, service_name
            """
        ),
        params,
    ).mappings()
    all_items = [
        {
            "name": str(row["service_name"]),
            "value": _money(row["list_cost"]),
        }
        for row in rows
    ]
    total = sum(item["value"] for item in all_items)
    for item in all_items:
        item["share_pct"] = rate_pct(item["value"], total)
        item["interactive"] = False
    return {
        "items": _share_items_above_threshold_with_others(
            all_items,
            min_share_pct=min_share_pct,
            total=total,
        ),
        "meta": {
            **filters.meta(),
            "min_share_pct": min_share_pct,
            "total_list_cost": round(total, 2),
        },
    }


def _budget_health_snapshot(
    connection: Connection,
    filters: CommonFilters,
) -> dict[str, Any] | None:
    today = _today()
    observed_through = today - timedelta(days=COST_DATA_LAG_DAYS)
    budget_period = _budget_period_for_filters(
        connection,
        filters,
        target_date=observed_through,
        allow_previous=True,
    )
    if budget_period is None:
        return None
    annual_budget = budget_period.amount
    period_start = budget_period.start_date
    period_end = budget_period.end_date
    observed_through = min(max(period_start, observed_through), period_end)
    current_scope = CommonFilters(
        start_date=period_start,
        end_date=observed_through,
        granularity=filters.granularity,
        cost_vendor=filters.cost_vendor,
        cost_account_id=filters.cost_account_id,
    )
    current_summary = _cost_summary(connection, current_scope)
    current_cost = current_summary["net_cost"]
    days_elapsed = max((observed_through - period_start).days + 1, 1)
    period_days = budget_period.days
    days_remaining = max((period_end - observed_through).days, 0)
    budget_to_date = round(annual_budget * days_elapsed / period_days, 2)
    variance = round(current_cost - budget_to_date, 2)

    recent_window_days = min(days_elapsed, FORECAST_WINDOW_DAYS)
    recent_window_start = observed_through - timedelta(days=recent_window_days - 1)
    recent_scope = CommonFilters(
        start_date=recent_window_start,
        end_date=observed_through,
        granularity=filters.granularity,
        cost_vendor=filters.cost_vendor,
        cost_account_id=filters.cost_account_id,
    )
    recent_summary = _cost_summary(connection, recent_scope)
    recent_window_cost = recent_summary["net_cost"]
    recent_daily_cost = round(recent_window_cost / recent_window_days, 2) if recent_window_days else 0.0
    forecast_remaining_cost = round(recent_daily_cost * days_remaining, 2)
    forecast_total_cost = round(current_cost + forecast_remaining_cost, 2)
    forecast_variance = round(forecast_total_cost - annual_budget, 2)
    is_healthy = forecast_total_cost <= annual_budget

    return {
        "metric_key": "net_cost",
        "annual_budget": round(annual_budget, 2),
        "period_budget": round(annual_budget, 2),
        "budget_start_date": period_start.isoformat(),
        "budget_end_date": period_end.isoformat(),
        "weekly_budget": round(annual_budget * 7 / period_days, 2),
        "budget_to_date": budget_to_date,
        "current_cost": current_cost,
        "through_date": observed_through.isoformat(),
        "days_elapsed": days_elapsed,
        "period_days": period_days,
        "days_remaining": days_remaining,
        "annual_budget_pct": rate_pct(current_cost, annual_budget),
        "budget_to_date_pct": rate_pct(current_cost, budget_to_date),
        "variance": variance,
        "variance_pct": rate_pct(variance, budget_to_date),
        "recent_window_days": recent_window_days,
        "recent_window_cost": recent_window_cost,
        "recent_daily_cost": recent_daily_cost,
        "forecast_remaining_cost": forecast_remaining_cost,
        "forecast_total_cost": forecast_total_cost,
        "forecast_budget_pct": rate_pct(forecast_total_cost, annual_budget),
        "forecast_variance": forecast_variance,
        "forecast_variance_pct": rate_pct(forecast_variance, annual_budget),
        "status": "healthy" if is_healthy else "warning",
        "status_label": "Healthy" if is_healthy else "Warning",
    }


def _share_items_above_threshold_with_others(
    all_items: list[dict[str, Any]],
    *,
    min_share_pct: float,
    total: float,
) -> list[dict[str, Any]]:
    items = [
        item
        for item in all_items
        if _number_or_zero(item.get("share_pct")) > min_share_pct
    ]
    if len(items) == len(all_items):
        return items

    others_value = total - sum(_number_or_zero(item.get("value")) for item in items)
    if others_value <= 0:
        return items

    return [
        *items,
        {
            "name": "Others",
            "value": _money(others_value),
            "share_pct": rate_pct(others_value, total),
            "interactive": False,
        },
    ]


def _share_items_limited_with_others(
    all_items: list[dict[str, Any]],
    *,
    limit: int,
    total: float,
) -> list[dict[str, Any]]:
    if limit < 2 or len(all_items) <= limit:
        return all_items[:limit]

    visible_items = all_items[: limit - 1]
    hidden_items = all_items[limit - 1 :]
    others_value = total - sum(_number_or_zero(item.get("value")) for item in visible_items)
    if others_value <= 0:
        return visible_items
    others_item = {
        "name": "Others",
        "value": _money(others_value),
        "share_pct": rate_pct(others_value, total),
        "interactive": False,
    }
    if any(item.get("highlight") for item in hidden_items):
        others_item["highlight"] = True

    return [*visible_items, others_item]


def _previous_window(filters: CommonFilters) -> tuple[date | None, date | None]:
    if filters.start_date is None or filters.end_date is None:
        return None, None
    span_days = max((filters.end_date - filters.start_date).days + 1, 1)
    previous_end = filters.start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span_days - 1)
    return previous_start, previous_end


def _number_or_zero(value: Any) -> float:
    return float(to_number(value) or 0)


def _cost_drilldown_filter(
    connection: Connection,
    *,
    child_group: str | None,
    drilldown_group: str | None,
    drilldown_value: str | None,
) -> dict[str, Any] | None:
    if not drilldown_group or drilldown_value is None:
        return None
    if child_group is not None and COST_DRILLDOWN_CHILD_GROUPS.get(drilldown_group) != child_group:
        return None

    parent_dimension = _cost_stack_dimension(connection, drilldown_group)
    return {
        "group": drilldown_group,
        "value": drilldown_value,
        "from_clause": parent_dimension["from_clause"],
        "condition": f"{parent_dimension['expr']} = :cost_drilldown_value",
        "params": {
            **parent_dimension["params"],
            "cost_drilldown_value": drilldown_value,
        },
    }


def _cost_dimension_meta(
    filters: CommonFilters,
    *,
    limit: int,
    dimension_key: str,
    dimension: str,
    drilldown: dict[str, Any] | None,
    total_list_cost: float | None = None,
    allocation_basis: str = CURRENT_ATTRIBUTION_BASIS,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        **filters.meta(),
        dimension_key: dimension,
        "limit": limit,
        "allocation_basis": allocation_basis,
    }
    if total_list_cost is not None:
        meta["total_list_cost"] = total_list_cost
    if drilldown:
        meta["drilldown_group"] = drilldown["group"]
        meta["drilldown_value"] = drilldown["value"]
    return meta


def _budget_period_for_filters(
    connection: Connection,
    filters: CommonFilters,
    *,
    target_date: date,
    allow_previous: bool = False,
) -> BudgetPeriod | None:
    if not filters.cost_vendor or not filters.cost_account_id:
        return None

    periods = _budget_periods_for_window(
        connection,
        filters,
        start_date=target_date,
        end_date=target_date,
    )
    period = _budget_period_for_date(periods, target_date)
    if period or not allow_previous:
        return period
    previous_periods = _budget_periods_for_window(
        connection,
        filters,
        start_date=target_date - timedelta(days=BUDGET_FALLBACK_MAX_DAYS),
        end_date=target_date - timedelta(days=1),
    )
    return _recent_previous_budget_period(previous_periods, target_date)


def _budget_targets_for_filters(
    connection: Connection,
    filters: CommonFilters,
    *,
    buckets: list[str],
) -> dict[str, float]:
    bucket_ranges = []
    for bucket in buckets:
        bucket_start = _parse_date(bucket)
        if bucket_start is None:
            continue
        bucket_ranges.append((bucket, bucket_start, _bucket_end(bucket_start, filters.granularity)))
    if not bucket_ranges:
        return {}
    periods = _budget_periods_for_window(
        connection,
        filters,
        start_date=min(start_date for _, start_date, _ in bucket_ranges),
        end_date=max(end_date for _, _, end_date in bucket_ranges),
    )
    targets: dict[str, float] = {}
    for bucket, bucket_start, bucket_end in bucket_ranges:
        target = _budget_amount_for_periods(periods, bucket_start, bucket_end)
        if target is not None:
            targets[bucket] = target
    return targets


def _budget_amount_for_periods(
    periods: list[BudgetPeriod],
    start_date: date,
    end_date: date,
) -> float | None:
    window_periods = [
        period
        for period in periods
        if period.start_date <= end_date and period.end_date >= start_date
    ]
    if not window_periods:
        return None
    return round(
        sum(_budget_amount_for_window(period, start_date, end_date) for period in window_periods),
        2,
    )


def _budget_periods_for_window(
    connection: Connection,
    filters: CommonFilters,
    *,
    start_date: date,
    end_date: date,
) -> list[BudgetPeriod]:
    if not filters.cost_vendor or not filters.cost_account_id:
        return []

    periods_by_account = _budget_periods_by_account_for_window(
        connection,
        start_date=start_date,
        end_date=end_date,
        vendor=filters.cost_vendor,
        account_id=filters.cost_account_id,
    )
    return periods_by_account.get((filters.cost_vendor, filters.cost_account_id), [])


def _budget_periods_by_account_for_window(
    connection: Connection,
    *,
    start_date: date,
    end_date: date,
    vendor: str | None = None,
    account_id: str | None = None,
) -> dict[tuple[str, str], list[BudgetPeriod]]:
    vendor_filter = "AND vendor = :vendor" if vendor else ""
    account_filter = "AND account_id = :account_id" if account_id else ""
    rows = connection.execute(
        text(
            f"""
            SELECT
              vendor,
              account_id,
              period_start_date,
              period_end_date,
              SUM(
                CASE
                  WHEN group_id IS NULL
                    AND manager_id IS NULL
                    AND repo IS NULL
                    AND label_filters IS NULL
                  THEN budget_amount
                  ELSE 0
                END
              ) AS source_wide_budget,
              SUM(
                CASE
                  WHEN group_id IS NULL
                    AND manager_id IS NULL
                    AND repo IS NULL
                    AND label_filters IS NULL
                  THEN 1
                  ELSE 0
                END
              ) AS source_wide_budget_count,
              SUM(
                CASE
                  WHEN group_id IS NULL AND manager_id IS NULL
                  THEN budget_amount
                  ELSE 0
                END
              ) AS fallback_budget,
              SUM(
                CASE
                  WHEN group_id IS NULL AND manager_id IS NULL
                  THEN 1
                  ELSE 0
                END
              ) AS fallback_budget_count
            FROM cost_budgets
            WHERE period_start_date <= :end_date
              AND period_end_date >= :start_date
              {vendor_filter}
              {account_filter}
            GROUP BY vendor, account_id, period_start_date, period_end_date
            ORDER BY vendor, account_id, period_start_date
            """
        ),
        {
            "vendor": vendor,
            "account_id": account_id,
            "start_date": start_date,
            "end_date": end_date,
        },
    ).mappings()

    periods_by_account: dict[tuple[str, str], list[BudgetPeriod]] = {}
    for row in rows:
        start = _parse_date(row["period_start_date"])
        end = _parse_date(row["period_end_date"])
        if start is None or end is None:
            continue
        if int(row["source_wide_budget_count"] or 0) > 0:
            amount = row["source_wide_budget"]
        elif int(row["fallback_budget_count"] or 0) > 0:
            amount = row["fallback_budget"]
        else:
            continue
        key = (str(row["vendor"]), str(row["account_id"]))
        periods_by_account.setdefault(key, []).append(BudgetPeriod(_money(amount), start, end))
    return periods_by_account


def _budget_period_for_date(periods: list[BudgetPeriod], target_date: date) -> BudgetPeriod | None:
    matching = [
        period
        for period in periods
        if period.start_date <= target_date <= period.end_date
    ]
    return max(matching, key=lambda period: period.start_date, default=None)


def _recent_previous_budget_period(periods: list[BudgetPeriod], target_date: date) -> BudgetPeriod | None:
    previous_period = max(periods, key=lambda period: period.end_date, default=None)
    if previous_period and (target_date - previous_period.end_date).days <= BUDGET_FALLBACK_MAX_DAYS:
        return previous_period
    return None


def _budget_amount_for_window(
    budget_period: BudgetPeriod,
    start_date: date,
    end_date: date,
) -> float:
    overlap_start = max(start_date, budget_period.start_date)
    overlap_end = min(end_date, budget_period.end_date)
    if overlap_start > overlap_end:
        return 0.0
    overlap_days = (overlap_end - overlap_start).days + 1
    return round(budget_period.amount * overlap_days / budget_period.days, 2)


def _budget_amount_for_days(budget_period: BudgetPeriod, days: int) -> float:
    return round(budget_period.amount * max(days, 0) / budget_period.days, 2)


def _cost_filters(filters: CommonFilters) -> CommonFilters:
    granularity = filters.granularity if filters.granularity in {"week", "month"} else "week"
    return CommonFilters(
        start_date=filters.start_date,
        end_date=filters.end_date,
        branch=filters.branch,
        granularity=granularity,
        cost_vendor=filters.cost_vendor,
        cost_account_id=filters.cost_account_id,
    )


def _cost_allocation_basis(
    connection: Connection,
    filters: CommonFilters,
    requested_basis: str,
) -> CostAllocationBasis:
    """Return replacement rows only when Kubernetes source lineage is complete."""
    if requested_basis not in VALID_COST_ALLOCATION_BASES:
        requested_basis = CURRENT_ATTRIBUTION_BASIS
    if requested_basis == CURRENT_ATTRIBUTION_BASIS or filters.granularity not in {
        "week",
        "month",
    }:
        return CostAllocationBasis(CURRENT_ATTRIBUTION_BASIS)

    materialized = _materialized_cost_basis(connection, filters, requested_basis)
    if materialized is not None:
        return materialized
    if requested_basis != RESIDUAL_ALLOCATED_BASIS:
        return CostAllocationBasis(CURRENT_ATTRIBUTION_BASIS)
    if not _cost_kubernetes_allocation_table_exists(connection):
        return CostAllocationBasis(CURRENT_ATTRIBUTION_BASIS)
    if not all(
        _table_has_column(connection, table_name, "source_summary_row_hash")
        for table_name in (
            "cost_attribution_daily",
            "cost_kubernetes_workload_allocation_daily",
        )
    ):
        return CostAllocationBasis(CURRENT_ATTRIBUTION_BASIS)

    cte, params = _residual_allocation_basis_cte(connection, filters)
    has_replacement = connection.execute(
        text(
            f"""
            {cte}
            SELECT 1
            FROM fully_allocated_sources
            LIMIT 1
            """
        ),
        params,
    ).first()
    if has_replacement is None and (
        _cost_kubernetes_allocation_source_table_exists(connection)
        and _table_has_column(
            connection,
            "cost_kubernetes_workload_allocation_daily",
            "allocation_group_hash",
        )
    ):
        has_replacement = connection.execute(
            text(
                f"""
                {cte}
                SELECT 1
                FROM fully_allocated_groups
                LIMIT 1
                """
            ),
            params,
        ).first()
    if has_replacement is None:
        return CostAllocationBasis(CURRENT_ATTRIBUTION_BASIS)
    return CostAllocationBasis(
        RESIDUAL_ALLOCATED_BASIS,
        from_clause="cost_basis c",
        cte=cte,
    )


def _materialized_cost_basis(
    connection: Connection,
    filters: CommonFilters,
    requested_basis: str,
) -> CostAllocationBasis | None:
    basis_key = MATERIALIZED_BASIS_KEYS.get(requested_basis)
    if basis_key is None or not all(
        _table_exists(connection, table_name)
        for table_name in ("cost_allocation_daily", "cost_allocation_publication")
    ):
        return None
    availability_filters = _cost_allocation_source_filters(filters)
    where_clause, params = _build_cost_where(availability_filters, table_alias="a")
    available = connection.execute(
        text(
            f"""
            SELECT 1
            FROM cost_allocation_daily a
            JOIN cost_allocation_publication p
              ON p.publication_name = 'dashboard'
             AND p.active_allocation_version = a.allocation_version
            WHERE a.basis_key = '{basis_key}' AND {where_clause}
            LIMIT 1
            """
        ),
        params,
    ).first()
    if available is None:
        return None
    return CostAllocationBasis(
        requested_basis,
        from_clause="cost_basis c",
        cte=f"""
            WITH cost_basis AS (
              SELECT a.*
              FROM cost_allocation_daily a
              JOIN cost_allocation_publication p
                ON p.publication_name = 'dashboard'
               AND p.active_allocation_version = a.allocation_version
              WHERE a.basis_key = '{basis_key}'
            )
        """,
        preserves_source_dimensions=True,
    )


def _residual_allocation_basis_cte(
    connection: Connection,
    filters: CommonFilters,
) -> tuple[str, dict[str, Any]]:
    # Source rows usually have no branch. Apply a branch filter only after a
    # workload allocation has supplied its workload branch dimension.
    source_where_clause, params = _build_cost_where(
        _cost_allocation_source_filters(filters),
        table_alias="source",
    )
    source_match = _allocation_source_match("allocation", "source")
    group_lineage_available = (
        _cost_kubernetes_allocation_source_table_exists(connection)
        and _table_has_column(
            connection,
            "cost_kubernetes_workload_allocation_daily",
            "allocation_group_hash",
        )
    )
    group_ctes = """
        , fully_allocated_group_sources AS (
          SELECT NULL AS attribution_id
          WHERE 1 = 0
        )
    """
    group_source_exclusion = ""
    group_allocation_rows = ""
    if group_lineage_available:
        mapping_where_clause, mapping_params = _build_cost_where(
            _cost_allocation_source_filters(filters),
            table_alias="mapping",
        )
        params.update(mapping_params)
        group_ctes = f"""
        , candidate_group_source_mappings AS (
          SELECT
            mapping.id AS mapping_id,
            mapping.allocation_group_hash,
            source.id AS attribution_id
          FROM cost_kubernetes_workload_allocation_source_daily mapping
          JOIN cost_attribution_daily source
            ON mapping.vendor = source.vendor
           AND mapping.account_id = source.account_id
           AND mapping.usage_date = source.usage_date
           AND mapping.source_summary_row_hash = source.source_summary_row_hash
          WHERE NULLIF(mapping.allocation_group_hash, '') IS NOT NULL
            AND NULLIF(source.source_summary_row_hash, '') IS NOT NULL
            AND {mapping_where_clause}
        ), single_source_group_mappings AS (
          SELECT
            mapping_id,
            MAX(allocation_group_hash) AS allocation_group_hash,
            MAX(attribution_id) AS attribution_id
          FROM candidate_group_source_mappings
          GROUP BY mapping_id
          HAVING COUNT(*) = 1
        ), group_source_coverage AS (
          SELECT
            mapping.allocation_group_hash,
            COUNT(*) AS source_mapping_count,
            COUNT(matched.mapping_id) AS matched_mapping_count,
            SUM(COALESCE(mapping.source_list_cost, 0)) AS mapped_source_list_cost
          FROM cost_kubernetes_workload_allocation_source_daily mapping
          LEFT JOIN single_source_group_mappings matched
            ON matched.mapping_id = mapping.id
          WHERE NULLIF(mapping.allocation_group_hash, '') IS NOT NULL
            AND {mapping_where_clause}
          GROUP BY mapping.allocation_group_hash
        ), group_source_totals AS (
          SELECT
            matched.allocation_group_hash,
            SUM(COALESCE(source.list_cost, 0)) AS source_list_cost,
            SUM(COALESCE(source.effective_cost, 0)) AS source_effective_cost,
            SUM(COALESCE(source.credit_amount, 0)) AS source_credit_amount,
            SUM(COALESCE(source.net_cost, 0)) AS source_net_cost,
            SUM(COALESCE(source.source_rows, 0)) AS source_rows
          FROM single_source_group_mappings matched
          JOIN cost_attribution_daily source
            ON source.id = matched.attribution_id
          GROUP BY matched.allocation_group_hash
        ), group_allocation_totals AS (
          SELECT
            allocation.allocation_group_hash,
            SUM(COALESCE(allocation.list_cost, 0)) AS allocated_list_cost
          FROM cost_kubernetes_workload_allocation_daily allocation
          JOIN group_source_coverage coverage
            ON coverage.allocation_group_hash = allocation.allocation_group_hash
          WHERE allocation.vendor = 'gcp'
            AND allocation.allocation_scope = 'workload_split'
            AND NULLIF(allocation.allocation_group_hash, '') IS NOT NULL
          GROUP BY allocation.allocation_group_hash
        ), fully_allocated_groups AS (
          SELECT coverage.allocation_group_hash
          FROM group_source_coverage coverage
          JOIN group_source_totals source
            ON source.allocation_group_hash = coverage.allocation_group_hash
          JOIN group_allocation_totals allocation
            ON allocation.allocation_group_hash = coverage.allocation_group_hash
          WHERE coverage.source_mapping_count = coverage.matched_mapping_count
            AND ABS(source.source_list_cost - coverage.mapped_source_list_cost) <= 0.005
            AND ABS(allocation.allocated_list_cost - coverage.mapped_source_list_cost) <= 0.005
        ), fully_allocated_group_sources AS (
          SELECT matched.attribution_id
          FROM single_source_group_mappings matched
          JOIN fully_allocated_groups allocated
            ON allocated.allocation_group_hash = matched.allocation_group_hash
        )
        """
        group_source_exclusion = """
            AND id NOT IN (SELECT attribution_id FROM fully_allocated_group_sources)
        """
        group_allocation_rows = """
          UNION ALL
          SELECT
            allocation.allocation_fact_id,
            allocation.usage_date,
            allocation.vendor,
            allocation.account_id,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            COALESCE(NULLIF(allocation.allocation_org, ''), NULL),
            COALESCE(NULLIF(allocation.allocation_repo, ''), NULL),
            allocation.allocation_target_branch,
            allocation.workload_name,
            NULL,
            allocation.allocation_scope,
            allocation.allocation_namespace,
            COALESCE(
              NULLIF(allocation.allocation_employee_email, ''),
              NULLIF(allocation.allocation_employee_github_id, ''),
              NULLIF(allocation.allocation_author, '')
            ),
            COALESCE(
              NULLIF(allocation.allocation_employee_email, ''),
              NULLIF(allocation.allocation_employee_github_id, '')
            ),
            NULL,
            NULL,
            NULL,
            CASE
              WHEN allocation.allocation_employee_id IS NULL THEN 'unattributed'
              ELSE __EMPLOYEE_ATTRIBUTION_KEY__
            END,
            CASE
              WHEN allocation.allocation_employee_id IS NULL THEN 'missing_author'
              ELSE 'kubernetes_residual_allocation'
            END,
            CASE
              WHEN allocation.allocation_employee_id IS NULL THEN 'unattributed'
              ELSE 'matched'
            END,
            allocation.allocation_method,
            allocation.allocation_employee_id,
            allocation.allocation_group_id,
            allocation.allocation_manager_id,
            NULL,
            allocation.allocated_list_cost,
            source.source_effective_cost * (
              CASE WHEN source.source_list_cost = 0 THEN 0
              ELSE allocation.allocated_list_cost / source.source_list_cost END
            ),
            source.source_credit_amount * (
              CASE WHEN source.source_list_cost = 0 THEN 0
              ELSE allocation.allocated_list_cost / source.source_list_cost END
            ),
            source.source_net_cost * (
              CASE WHEN source.source_list_cost = 0 THEN 0
              ELSE allocation.allocated_list_cost / source.source_list_cost END
            ),
            source.source_rows,
            allocation.allocation_dimension_hash,
            NULL
          FROM ranked_group_allocations allocation
          JOIN group_source_totals source
            ON source.allocation_group_hash = allocation.allocation_group_hash
          WHERE allocation.roster_match_rank = 1
        """
    source_columns = """
        id, usage_date, vendor, account_id, service_name, sku_name, usage_type,
        cost_driver_key, region, org, repo, target_branch, resource_name,
        vendor_tags_json, source_allocation_scope, namespace, author, owner,
        service, project, service_exec_id, attribution_key, attribution_source,
        attribution_status, allocate_method, employee_id, group_id, manager_id,
        usage_seconds, list_cost, effective_cost, credit_amount, net_cost,
        source_rows, dimension_hash, source_summary_row_hash
    """
    allocated_cost_ratio = (
        "CASE WHEN COALESCE(source.list_cost, 0) = 0 THEN 0 "
        "ELSE allocation.allocated_list_cost / source.list_cost END"
    )
    employee_attribution_key = (
        "'employee:' || allocation.allocation_employee_id"
        if connection.dialect.name == "sqlite"
        else "CONCAT('employee:', allocation.allocation_employee_id)"
    )
    cte = f"""
        WITH candidate_allocation_facts AS (
          SELECT allocation.id AS allocation_fact_id, source.id AS attribution_id
          FROM cost_kubernetes_workload_allocation_daily allocation
          JOIN cost_attribution_daily source
            ON {source_match}
          WHERE allocation.allocation_scope IN ('workload_split', 'unallocated')
            AND NULLIF(allocation.source_summary_row_hash, '') IS NOT NULL
            AND NULLIF(source.source_summary_row_hash, '') IS NOT NULL
            AND {source_where_clause}
        ), single_source_facts AS (
          SELECT allocation_fact_id, MAX(attribution_id) AS attribution_id
          FROM candidate_allocation_facts
          GROUP BY allocation_fact_id
          HAVING COUNT(*) = 1
        ), matched_allocation_facts AS (
          SELECT allocation_fact_id, attribution_id
          FROM single_source_facts
        ), fully_allocated_sources AS (
          SELECT matched.attribution_id
          FROM matched_allocation_facts matched
          JOIN cost_kubernetes_workload_allocation_daily allocation
            ON allocation.id = matched.allocation_fact_id
          JOIN cost_attribution_daily source
            ON source.id = matched.attribution_id
          GROUP BY matched.attribution_id
          HAVING ABS(
            SUM(COALESCE(allocation.list_cost, 0))
            - MAX(COALESCE(source.list_cost, 0))
          ) <= 0.005
        ){group_ctes}, active_roster_identities AS (
          SELECT
            id AS employee_id,
            email,
            github_id,
            group_id,
            manager_id,
            LOWER(NULLIF(email, '')) AS identity,
            0 AS identity_priority
          FROM roster_employees
          WHERE is_active = 1
            AND NULLIF(email, '') IS NOT NULL
          UNION ALL
          SELECT
            id AS employee_id,
            email,
            github_id,
            group_id,
            manager_id,
            LOWER(NULLIF(github_id, '')) AS identity,
            1 AS identity_priority
          FROM roster_employees
          WHERE is_active = 1
            AND NULLIF(github_id, '') IS NOT NULL
        ), ranked_allocations AS (
          SELECT
            allocation.id AS allocation_fact_id,
            source.id AS attribution_id,
            allocation.cluster_location,
            allocation.allocation_scope,
            allocation.namespace AS allocation_namespace,
            allocation.workload_name,
            allocation.author AS allocation_author,
            allocation.org AS allocation_org,
            allocation.repo AS allocation_repo,
            allocation.target_branch AS allocation_target_branch,
            allocation.list_cost AS allocated_list_cost,
            allocation.allocation_method,
            allocation.dimension_hash AS allocation_dimension_hash,
            roster.employee_id AS allocation_employee_id,
            roster.email AS allocation_employee_email,
            roster.github_id AS allocation_employee_github_id,
            roster.group_id AS allocation_group_id,
            roster.manager_id AS allocation_manager_id,
            ROW_NUMBER() OVER (
              PARTITION BY allocation.id
              ORDER BY COALESCE(roster.identity_priority, 2), roster.employee_id
            ) AS roster_match_rank
          FROM matched_allocation_facts matched
          JOIN fully_allocated_sources full_source
            ON full_source.attribution_id = matched.attribution_id
          JOIN cost_kubernetes_workload_allocation_daily allocation
            ON allocation.id = matched.allocation_fact_id
          JOIN cost_attribution_daily source
            ON source.id = matched.attribution_id
          LEFT JOIN active_roster_identities roster
            ON roster.identity = LOWER(NULLIF(allocation.author, ''))
        ){_ranked_group_allocations_cte(group_lineage_available)}, allocated_rows AS (
          SELECT {source_columns}
          FROM cost_attribution_daily source
          WHERE {source_where_clause}
            AND id NOT IN (SELECT attribution_id FROM fully_allocated_sources)
            {group_source_exclusion}
          UNION ALL
          SELECT
            source.id,
            source.usage_date,
            source.vendor,
            source.account_id,
            source.service_name,
            source.sku_name,
            source.usage_type,
            source.cost_driver_key,
            COALESCE(NULLIF(allocation.cluster_location, ''), source.region),
            COALESCE(NULLIF(allocation.allocation_org, ''), source.org),
            COALESCE(NULLIF(allocation.allocation_repo, ''), source.repo),
            COALESCE(NULLIF(allocation.allocation_target_branch, ''), source.target_branch),
            COALESCE(NULLIF(allocation.workload_name, ''), source.resource_name),
            source.vendor_tags_json,
            allocation.allocation_scope,
            COALESCE(NULLIF(allocation.allocation_namespace, ''), source.namespace),
            COALESCE(
              NULLIF(allocation.allocation_employee_email, ''),
              NULLIF(allocation.allocation_employee_github_id, ''),
              NULLIF(allocation.allocation_author, '')
            ),
            COALESCE(
              NULLIF(allocation.allocation_employee_email, ''),
              NULLIF(allocation.allocation_employee_github_id, '')
            ),
            source.service,
            source.project,
            source.service_exec_id,
            CASE
              WHEN allocation.allocation_employee_id IS NULL THEN source.attribution_key
              ELSE {employee_attribution_key}
            END,
            CASE
              WHEN allocation.allocation_employee_id IS NULL THEN source.attribution_source
              ELSE 'kubernetes_residual_allocation'
            END,
            CASE
              WHEN allocation.allocation_employee_id IS NULL THEN source.attribution_status
              ELSE 'matched'
            END,
            allocation.allocation_method,
            allocation.allocation_employee_id,
            allocation.allocation_group_id,
            allocation.allocation_manager_id,
            source.usage_seconds,
            allocation.allocated_list_cost,
            source.effective_cost * ({allocated_cost_ratio}),
            source.credit_amount * ({allocated_cost_ratio}),
            source.net_cost * ({allocated_cost_ratio}),
            source.source_rows,
            allocation.allocation_dimension_hash,
            source.source_summary_row_hash
          FROM ranked_allocations allocation
          JOIN cost_attribution_daily source
            ON source.id = allocation.attribution_id
          WHERE allocation.roster_match_rank = 1
          {group_allocation_rows.replace("__EMPLOYEE_ATTRIBUTION_KEY__", employee_attribution_key)}
        ), cost_basis AS (
          SELECT {source_columns}
          FROM allocated_rows
        )
    """
    return cte, params


def _ranked_group_allocations_cte(group_lineage_available: bool) -> str:
    if not group_lineage_available:
        return ""
    return """
        , ranked_group_allocations AS (
          SELECT
            allocation.id AS allocation_fact_id,
            allocation.allocation_group_hash,
            allocation.usage_date,
            allocation.vendor,
            allocation.account_id,
            allocation.cluster_location,
            allocation.allocation_scope,
            allocation.namespace AS allocation_namespace,
            allocation.workload_name,
            allocation.author AS allocation_author,
            allocation.org AS allocation_org,
            allocation.repo AS allocation_repo,
            allocation.target_branch AS allocation_target_branch,
            allocation.list_cost AS allocated_list_cost,
            allocation.allocation_method,
            allocation.dimension_hash AS allocation_dimension_hash,
            roster.employee_id AS allocation_employee_id,
            roster.email AS allocation_employee_email,
            roster.github_id AS allocation_employee_github_id,
            roster.group_id AS allocation_group_id,
            roster.manager_id AS allocation_manager_id,
            ROW_NUMBER() OVER (
              PARTITION BY allocation.id
              ORDER BY COALESCE(roster.identity_priority, 2), roster.employee_id
            ) AS roster_match_rank
          FROM fully_allocated_groups allocated_group
          JOIN cost_kubernetes_workload_allocation_daily allocation
            ON allocation.allocation_group_hash = allocated_group.allocation_group_hash
          LEFT JOIN active_roster_identities roster
            ON roster.identity = LOWER(NULLIF(allocation.author, ''))
        )
    """


def _cost_allocation_source_filters(filters: CommonFilters) -> CommonFilters:
    return CommonFilters(
        start_date=filters.start_date,
        end_date=filters.end_date,
        granularity=filters.granularity,
        cost_vendor=filters.cost_vendor,
        cost_account_id=filters.cost_account_id,
    )


def _allocation_source_match(allocation_alias: str, attribution_alias: str) -> str:
    return f"""
        {allocation_alias}.vendor = {attribution_alias}.vendor
        AND {allocation_alias}.account_id = {attribution_alias}.account_id
        AND {allocation_alias}.usage_date = {attribution_alias}.usage_date
        AND {allocation_alias}.source_summary_row_hash
            = {attribution_alias}.source_summary_row_hash
    """


def _cost_basis_from_clause(basis: CostAllocationBasis, from_clause: str) -> str:
    return from_clause.replace("cost_attribution_daily c", basis.from_clause, 1)


def _cost_basis_for_dimension(
    basis: CostAllocationBasis,
    dimension: str | None,
) -> CostAllocationBasis:
    """Keep billing-source dimensions on their original attribution rows.

    Kubernetes allocation changes who owns a cost, not its provider service,
    SKU, billing project, execution id, or billing region. Grouped allocation
    facts intentionally do not carry those source attributes.
    """
    if dimension in SOURCE_COST_DIMENSIONS and not basis.preserves_source_dimensions:
        return CostAllocationBasis(basis.name)
    return basis


def _cost_basis_dimension(
    basis: CostAllocationBasis,
    dimension: dict[str, Any],
) -> dict[str, Any]:
    return {
        **dimension,
        "from_clause": _cost_basis_from_clause(basis, dimension["from_clause"]),
    }


def _table_exists(connection: Connection, table_name: str) -> bool:
    if connection.dialect.name == "sqlite":
        return connection.execute(
            text(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = :table_name
                """
            ),
            {"table_name": table_name},
        ).first() is not None
    return connection.execute(
        text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    ).first() is not None


def _table_has_column(connection: Connection, table_name: str, column_name: str) -> bool:
    if connection.dialect.name == "sqlite":
        rows = connection.execute(text(f"PRAGMA table_info({table_name})")).mappings()
        return any(row["name"] == column_name for row in rows)
    row = connection.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND column_name = :column_name
            LIMIT 1
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).first()
    return row is not None


def _build_cost_where(
    filters: CommonFilters,
    *,
    table_alias: str = "",
) -> tuple[str, dict[str, Any]]:
    prefix = f"{table_alias}." if table_alias else ""
    conditions = ["1=1"]
    params: dict[str, Any] = {}
    if filters.start_date:
        conditions.append(f"{prefix}usage_date >= :usage_date_from")
        params["usage_date_from"] = filters.start_date
    if filters.end_date:
        conditions.append(f"{prefix}usage_date <= :usage_date_to")
        params["usage_date_to"] = filters.end_date
    if filters.cost_vendor:
        conditions.append(f"{prefix}vendor = :cost_vendor")
        params["cost_vendor"] = filters.cost_vendor
    if filters.cost_account_id:
        conditions.append(f"{prefix}account_id = :cost_account_id")
        params["cost_account_id"] = filters.cost_account_id
    if filters.branch:
        conditions.append(f"{prefix}target_branch = :branch")
        params["branch"] = filters.branch
    return " AND ".join(conditions), params


def _cost_attribution_index_hint(
    connection: Connection,
    filters: CommonFilters,
    table_alias: str = "c",
) -> str:
    return _source_date_index_hint(
        connection,
        filters,
        table_alias=table_alias,
        index_name=COST_ATTRIBUTION_SOURCE_DATE_INDEX,
    )


def _cost_aggregate_read_hint(
    connection: Connection,
    filters: CommonFilters,
    *,
    table_alias: str = "c",
) -> str:
    if (
        connection.dialect.name != "sqlite"
        and (filters.cost_vendor, filters.cost_account_id) in TIFLASH_COST_SOURCES
        and filters.start_date
        and filters.end_date
    ):
        return f"/*+ READ_FROM_STORAGE(TIFLASH[{table_alias}]) */"
    return _cost_attribution_index_hint(connection, filters, table_alias)


def _cost_basis_index_hint(
    connection: Connection,
    filters: CommonFilters,
    basis: CostAllocationBasis,
) -> str:
    if basis.from_clause != "cost_attribution_daily c":
        return ""
    return _cost_aggregate_read_hint(connection, filters)


def _cost_kubernetes_allocation_read_hint(
    connection: Connection,
    filters: CommonFilters,
    *,
    table_alias: str = "a",
) -> str:
    if (
        connection.dialect.name != "sqlite"
        and (filters.cost_vendor, filters.cost_account_id) in TIFLASH_COST_SOURCES
        and filters.start_date
        and filters.end_date
    ):
        return f"/*+ READ_FROM_STORAGE(TIFLASH[{table_alias}]) */"
    return _cost_kubernetes_allocation_index_hint(connection, filters, table_alias)


def _cost_kubernetes_allocation_index_hint(
    connection: Connection,
    filters: CommonFilters,
    table_alias: str = "a",
) -> str:
    return _source_date_index_hint(
        connection,
        filters,
        table_alias=table_alias,
        index_name=COST_KUBERNETES_ALLOCATION_SOURCE_DATE_INDEX,
    )


def _cost_unmatched_resource_index_hint(
    connection: Connection,
    filters: CommonFilters,
    table_alias: str = "r",
) -> str:
    return _source_date_index_hint(
        connection,
        filters,
        table_alias=table_alias,
        index_name=COST_UNMATCHED_SOURCE_DATE_NAMESPACE_INDEX,
    )


def _source_date_index_hint(
    connection: Connection,
    filters: CommonFilters,
    *,
    table_alias: str,
    index_name: str,
) -> str:
    if connection.dialect.name == "sqlite":
        return ""
    if not (filters.cost_vendor and filters.cost_account_id):
        return ""
    if not (filters.start_date or filters.end_date):
        return ""
    return f"/*+ USE_INDEX({table_alias}, {index_name}) */"


def _billing_report_list_cost_expr(table_alias: str) -> str:
    prefix = f"{table_alias}." if table_alias else ""
    return (
        "CASE "
        f"WHEN {prefix}vendor = 'gcp' "
        f"AND {prefix}sku_name LIKE 'Compute Flexible Committed Use Discounts%' "
        "THEN 0 "
        f"ELSE {prefix}list_cost "
        "END"
    )


def _kubernetes_parent_residual_condition(table_alias: str) -> str:
    return f"""
        (
          {table_alias}.source_allocation_scope IN (
            'kubernetes_parent_residual',
            'eks_parent_residual',
            'eks_unallocated',
            'gke_parent_residual',
            'gke_residual',
            'tke_parent_residual'
          )
          -- Older AWS refreshes emitted residuals as direct rows but retained
          -- their parent-residual SKU or usage type.
          OR (
            {table_alias}.source_allocation_scope = 'direct'
            AND (
              LOWER(COALESCE({table_alias}.sku_name, '')) LIKE '%parentresidual'
              OR LOWER(COALESCE({table_alias}.usage_type, '')) LIKE '%parentresidual'
            )
          )
        )
    """


def _kubernetes_service_cost_condition(table_alias: str) -> str:
    service_name = f"{table_alias}.service_name"
    return f"""
        (
          {service_name} = 'AmazonEKS'
          OR LOWER(COALESCE({service_name}, '')) LIKE '%kubernetes%'
          OR LOWER(COALESCE({service_name}, '')) LIKE '%container engine%'
        )
    """


def _kubernetes_allocation_fact_service_expr(table_alias: str) -> str:
    return f"""
        CASE
          WHEN LOWER(COALESCE({table_alias}.cost_component, '')) = 'control_plane'
            THEN CASE {table_alias}.vendor
              WHEN 'aws' THEN 'AmazonEKS'
              WHEN 'gcp' THEN 'Kubernetes Engine'
              WHEN 'tencent' THEN 'Tencent Kubernetes Engine'
              ELSE '(allocation fact)'
            END
          ELSE CASE {table_alias}.vendor
            WHEN 'aws' THEN 'AmazonEC2'
            WHEN 'gcp' THEN 'Compute Engine'
            WHEN 'tencent' THEN 'Cloud Virtual Machine'
            ELSE '(allocation fact)'
          END
        END
    """


def _has_valid_legacy_person_attribution(table_alias: str) -> str:
    return f"""
        (
          {table_alias}.employee_id IS NOT NULL
          AND LOWER(COALESCE({table_alias}.attribution_status, '')) = 'matched'
        )
    """


def _has_valid_allocation_fact_person_attribution(
    table_alias: str,
    active_roster_alias: str | None = None,
) -> str:
    if active_roster_alias:
        return f"{active_roster_alias}.identity IS NOT NULL"
    return f"""
        (
          NULLIF({table_alias}.author, '') IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM roster_employees employee
            WHERE employee.is_active = 1
              AND (
                LOWER(NULLIF(employee.email, '')) = LOWER(NULLIF({table_alias}.author, ''))
                OR LOWER(NULLIF(employee.github_id, '')) = LOWER(NULLIF({table_alias}.author, ''))
              )
          )
        )
    """


def _kubernetes_allocation_fact_control_plane_condition(table_alias: str) -> str:
    return f"""
        LOWER(COALESCE({table_alias}.cost_component, '')) = 'control_plane'
    """


def _kubernetes_allocation_fact_allocated_condition(
    table_alias: str,
    active_roster_alias: str | None = None,
) -> str:
    return f"""
        (
          {table_alias}.allocation_scope = 'workload_split'
          OR (
          {table_alias}.allocation_scope = 'unallocated'
            AND {_has_valid_allocation_fact_person_attribution(table_alias, active_roster_alias)}
            AND NOT {_kubernetes_allocation_fact_control_plane_condition(table_alias)}
          )
        )
    """


def _kubernetes_allocation_fact_unallocated_condition(
    table_alias: str,
    active_roster_alias: str | None = None,
) -> str:
    return f"""
        (
          {table_alias}.allocation_scope = 'unallocated'
          AND NOT {_has_valid_allocation_fact_person_attribution(table_alias, active_roster_alias)}
        )
    """


def _kubernetes_allocation_fact_active_roster_cte() -> str:
    """Return active roster identities once for joins against allocation facts."""
    return """
        active_roster_identities AS (
          SELECT LOWER(NULLIF(email, '')) AS identity
          FROM roster_employees
          WHERE is_active = 1
            AND NULLIF(email, '') IS NOT NULL
          UNION
          SELECT LOWER(NULLIF(github_id, '')) AS identity
          FROM roster_employees
          WHERE is_active = 1
            AND NULLIF(github_id, '') IS NOT NULL
        )
    """


def _kubernetes_allocation_fact_roster_join(
    table_alias: str,
    active_roster_alias: str,
) -> str:
    return f"""
        LEFT JOIN active_roster_identities {active_roster_alias}
          ON {active_roster_alias}.identity = LOWER(NULLIF({table_alias}.author, ''))
    """


def _kubernetes_unallocated_condition(connection: Connection, table_alias: str) -> str:
    return f"""
        (
          (
            {_kubernetes_parent_residual_condition(table_alias)}
            AND NOT {_has_valid_legacy_person_attribution(table_alias)}
          )
          OR {_kubernetes_direct_unallocated_condition(connection, table_alias)}
        )
    """


def _kubernetes_direct_unallocated_condition(connection: Connection, table_alias: str) -> str:
    cluster_tag = _json_tag_text_expr(connection, f"{table_alias}.vendor_tags_json", "cluster")
    return f"""
        (
          {table_alias}.source_allocation_scope IN ('direct', 'gke_direct')
          AND NOT {_has_valid_legacy_person_attribution(table_alias)}
          AND (
            {_kubernetes_service_cost_condition(table_alias)}
            -- A user-managed cluster tag is not proof that an AWS resource is
            -- an EKS node. AWS unsplit nodes need a provider-native identity
            -- before they can enter this view.
            OR (
              COALESCE({table_alias}.vendor, '') <> 'aws'
              AND NULLIF({cluster_tag}, '') IS NOT NULL
            )
          )
        )
    """


def _cost_kubernetes_allocation_table_exists(connection: Connection) -> bool:
    if connection.dialect.name == "sqlite":
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'cost_kubernetes_workload_allocation_daily'
                """
            )
        ).first()
    else:
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 'cost_kubernetes_workload_allocation_daily'
                """
            )
        ).first()
    return row is not None


def _cost_billing_summary_table_exists(connection: Connection) -> bool:
    if connection.dialect.name == "sqlite":
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'cost_bq_export_summary_daily'
                """
            )
        ).first()
    else:
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 'cost_bq_export_summary_daily'
                """
            )
        ).first()
    return row is not None


def _cost_kubernetes_allocation_source_table_exists(connection: Connection) -> bool:
    if not hasattr(connection, "execute"):
        return False
    if connection.dialect.name == "sqlite":
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'cost_kubernetes_workload_allocation_source_daily'
                LIMIT 1
                """
            )
        ).first()
        return row is not None
    row = connection.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = 'cost_kubernetes_workload_allocation_source_daily'
            LIMIT 1
            """
        )
    ).first()
    return row is not None


def _json_tag_text_expr(connection: Connection, column_expr: str, tag_name: str) -> str:
    json_path = f"$.{tag_name}"
    if connection.dialect.name == "sqlite":
        return f"json_extract({column_expr}, '{json_path}')"
    return f"JSON_UNQUOTE(JSON_EXTRACT({column_expr}, '{json_path}'))"


def _json_text_expr(connection: Connection, column_expr: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"COALESCE({column_expr}, '')"
    return f"COALESCE(CAST({column_expr} AS CHAR), '')"


def _format_vendor_labels(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(parsed, Mapping):
        return raw

    labels = []
    for key, label_value in sorted(parsed.items()):
        if label_value in (None, ""):
            continue
        text_value = (
            json.dumps(label_value, sort_keys=True)
            if isinstance(label_value, (dict, list))
            else str(label_value)
        )
        labels.append(f"{key}={text_value}")
    return ", ".join(labels)


def _user_facing_dimension(value: Any) -> str:
    """Hide opaque numeric identifiers while preserving recognizable names."""
    dimension = str(value or "").strip()
    return "" if not dimension or dimension.isdecimal() else dimension


def _like_prefix_expr(connection: Connection, value_expr: str, prefix_expr: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"{value_expr} LIKE {prefix_expr} || '%'"
    return f"{value_expr} LIKE CONCAT({prefix_expr}, '%')"


def _null_safe_eq(connection: Connection, left_expr: str, right_expr: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"{left_expr} IS {right_expr}"
    return f"{left_expr} <=> {right_expr}"


def _cost_stack_dimension(connection: Connection, group_by: str) -> dict[str, Any]:
    if group_by not in VALID_COST_STACK_GROUPS:
        group_by = "repo"

    if group_by == "author":
        return {
            "expr": "COALESCE(NULLIF(c.author, ''), '(unknown author)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(unknown author)",
        }
    if group_by == "owner":
        return {
            "expr": "COALESCE(NULLIF(c.owner, ''), '(no owner)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no owner)",
        }
    if group_by == "team":
        team_match = _like_prefix_expr(connection, "c_group.path", "target_group.path")
        return {
            "expr": "COALESCE(NULLIF(target_group.name, ''), '(no team)')",
            "from_clause": f"""
                cost_attribution_daily c
                LEFT JOIN roster_groups c_group
                  ON c_group.id = c.group_id
                LEFT JOIN (
                  SELECT target_group.name, target_group.path
                  FROM roster_groups root_group
                  JOIN roster_groups target_parent
                    ON target_parent.is_active = 1
                   AND target_parent.parent_id = root_group.id
                  JOIN roster_groups target_group
                    ON target_group.is_active = 1
                   AND target_group.parent_id = target_parent.id
                  WHERE root_group.name = :cost_stack_root_group_name
                    AND root_group.is_active = 1
                ) target_group
                  ON c_group.path IS NOT NULL
                 AND {team_match}
            """,
            "params": {"cost_stack_root_group_name": ENGINEERING_GROUP_NAME},
            "empty_label": "(no team)",
        }
    if group_by == "target_branch":
        return {
            "expr": "COALESCE(NULLIF(c.target_branch, ''), '(no target branch)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no target branch)",
        }
    if group_by == "service":
        return {
            "expr": _cost_service_share_expr("c"),
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no service)",
        }
    if group_by == "sku":
        return {
            "expr": _cost_sku_share_expr(connection, "c"),
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no SKU)",
        }
    if group_by == "cost_driver":
        return {
            "expr": _cost_driver_share_expr("c"),
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "Other",
        }
    if group_by == "project":
        return {
            "expr": "COALESCE(NULLIF(c.project, ''), '(no project)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no project)",
        }
    if group_by == "region":
        return {
            "expr": "COALESCE(NULLIF(c.region, ''), '(no region)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no region)",
        }
    if group_by == "service_exec_id":
        return {
            "expr": "COALESCE(NULLIF(c.service_exec_id, ''), '(no service exec id)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no service exec id)",
        }
    return {
        "expr": "COALESCE(NULLIF(c.repo, ''), '(no repo)')",
        "from_clause": "cost_attribution_daily c",
        "params": {},
        "empty_label": "(no repo)",
    }


def _cost_share_dimension(connection: Connection, dimension: str) -> dict[str, Any]:
    if dimension not in VALID_COST_SHARE_DIMENSIONS:
        dimension = "owner"

    if dimension == "owner":
        return {
            "expr": "COALESCE(NULLIF(c.owner, ''), '(no owner)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no owner)",
        }
    if dimension == "team":
        return _cost_stack_dimension(connection, "team")
    if dimension == "service":
        return {
            "expr": _cost_service_share_expr("c"),
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no service)",
        }
    if dimension == "sku":
        return {
            "expr": _cost_sku_share_expr(connection, "c"),
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no SKU)",
        }
    if dimension == "cost_driver":
        return {
            "expr": _cost_driver_share_expr("c"),
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "Other",
        }
    if dimension == "project":
        return {
            "expr": "COALESCE(NULLIF(c.project, ''), '(no project)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no project)",
        }
    if dimension == "region":
        return {
            "expr": "COALESCE(NULLIF(c.region, ''), '(no region)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no region)",
        }
    return {
        "expr": "COALESCE(NULLIF(c.service_exec_id, ''), '(no service exec id)')",
        "from_clause": "cost_attribution_daily c",
        "params": {},
        "empty_label": "(no service exec id)",
    }


def _cost_service_share_expr(table_alias: str) -> str:
    prefix = f"{table_alias}." if table_alias else ""
    service = f"{prefix}service_name"
    sku = f"{prefix}sku_name"
    return (
        "CASE "
        f"WHEN LOWER(COALESCE({service}, '')) = 'amazonec2' "
        f"AND (LOWER(COALESCE({sku}, '')) LIKE '%ebs%' "
        f"OR LOWER(COALESCE({sku}, '')) LIKE '%volume%' "
        f"OR LOWER(COALESCE({sku}, '')) LIKE '%snapshot%' "
        f"OR LOWER(COALESCE({sku}, '')) LIKE '%elastic block store%') "
        "THEN 'EBS' "
        f"WHEN LOWER(COALESCE({service}, '')) = 'amazonec2' THEN 'EC2' "
        f"WHEN LOWER(COALESCE({service}, '')) = 'amazons3' THEN 'S3' "
        f"WHEN NULLIF({service}, '') IS NULL THEN '(no service)' "
        f"ELSE {service} "
        "END"
    )


def _cost_sku_share_expr(connection: Connection, table_alias: str) -> str:
    prefix = f"{table_alias}." if table_alias else ""
    usage = f"NULLIF({prefix}usage_type, '')"
    sku = f"NULLIF({prefix}sku_name, '')"
    if connection.dialect.name == "sqlite":
        readable_usage = (
            "CASE "
            f"WHEN {prefix}vendor = 'aws' "
            f"AND COALESCE({prefix}cost_driver_key, '') = 'compute' "
            f"AND {usage} LIKE '%-BoxUsage:%' "
            f"THEN substr({usage}, instr({usage}, '-BoxUsage:') + length('-BoxUsage:')) "
            f"WHEN {prefix}vendor = 'aws' "
            f"AND COALESCE({prefix}cost_driver_key, '') = 'compute' "
            f"AND {usage} LIKE 'BoxUsage:%' "
            f"THEN substr({usage}, length('BoxUsage:') + 1) "
            f"ELSE {usage} "
            "END"
        )
    else:
        readable_usage = (
            "CASE "
            f"WHEN {prefix}vendor = 'aws' "
            f"AND COALESCE({prefix}cost_driver_key, '') = 'compute' "
            f"AND {usage} LIKE '%-BoxUsage:%' "
            f"THEN SUBSTRING({usage}, LOCATE('-BoxUsage:', {usage}) + LENGTH('-BoxUsage:')) "
            f"WHEN {prefix}vendor = 'aws' "
            f"AND COALESCE({prefix}cost_driver_key, '') = 'compute' "
            f"AND {usage} LIKE 'BoxUsage:%' "
            f"THEN SUBSTRING({usage}, LENGTH('BoxUsage:') + 1) "
            f"ELSE {usage} "
            "END"
        )
    return f"COALESCE({readable_usage}, {sku}, '(no SKU)')"


def _cost_driver_share_expr(table_alias: str) -> str:
    prefix = f"{table_alias}." if table_alias else ""
    key = f"COALESCE(NULLIF({prefix}cost_driver_key, ''), 'other')"
    branches = " ".join(
        f"WHEN {key} = '{driver_key}' THEN '{label}'"
        for driver_key, label in COST_DRIVER_LABELS.items()
        if driver_key != "other"
    )
    return (
        f"CASE {branches} "
        f"ELSE '{COST_DRIVER_LABELS['other']}' "
        "END"
    )


def _cost_stack_key(group_by: str, dimension_name: str, index: int) -> str:
    if group_by == "repo" and dimension_name == "(no repo)":
        return "repo__no_repo"
    if group_by == "author" and dimension_name == "(unknown author)":
        return "author__unknown_author"
    if group_by == "owner" and dimension_name == "(no owner)":
        return "owner__no_owner"
    if group_by == "team" and dimension_name == "(no team)":
        return "team__no_team"
    if group_by == "target_branch" and dimension_name == "(no target branch)":
        return "target_branch__no_target_branch"
    if group_by == "service" and dimension_name == "(no service)":
        return "service__no_service"
    if group_by == "cost_driver" and dimension_name == "Other":
        return "cost_driver__other"
    if group_by == "project" and dimension_name == "(no project)":
        return "project__no_project"
    if group_by == "service_exec_id" and dimension_name == "(no service exec id)":
        return "service_exec_id__no_service_exec_id"
    return f"{group_by}__{index}"


def _cost_source_value(vendor: str, account_id: str) -> str:
    return f"{vendor}:{account_id}"


def _cost_source_label(vendor: str, account_id: str) -> str:
    return f"{vendor} / {account_id}"


def _bucket_starts(filters: CommonFilters, rows: list[dict[str, Any]]) -> list[str]:
    if filters.start_date and filters.end_date:
        if filters.granularity == "month":
            return _month_bucket_starts(filters.start_date, filters.end_date)
        return _week_bucket_starts(filters.start_date, filters.end_date)
    return sorted({str(row["bucket_start"]) for row in rows})


def _week_bucket_starts(start_date: date, end_date: date) -> list[str]:
    cursor = start_date - timedelta(days=start_date.weekday())
    buckets: list[str] = []
    while cursor <= end_date:
        buckets.append(cursor.isoformat())
        cursor += timedelta(days=7)
    return buckets


def _month_bucket_starts(start_date: date, end_date: date) -> list[str]:
    cursor = start_date.replace(day=1)
    end_bucket = end_date.replace(day=1)
    buckets: list[str] = []
    while cursor <= end_bucket:
        buckets.append(cursor.isoformat())
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return buckets


def _bucket_end(bucket_start: date, granularity: str) -> date:
    if granularity == "month":
        last_day = calendar.monthrange(bucket_start.year, bucket_start.month)[1]
        return bucket_start.replace(day=last_day)
    return bucket_start + timedelta(days=6)


def _resource_labels(row: Mapping[str, Any]) -> str:
    pairs = []
    seen_keys = set()
    for key, value in _resource_vendor_tag_pairs(row.get("vendor_tags_json")):
        pairs.append(f"{key}={value}")
        seen_keys.add(key)

    for key, label in (
        ("org_name", "org"),
        ("repo_name", "repo"),
        ("target_branch", "branch"),
        ("author_name", "author"),
        ("owner_mail", "owner_mail"),
    ):
        value = str(row[key] or "").strip()
        if value and label not in seen_keys:
            pairs.append(f"{label}={value}")
            seen_keys.add(label)
    return ", ".join(pairs)


def _resource_vendor_tag_pairs(value: Any) -> list[tuple[str, str]]:
    if value is None:
        return []

    raw_tags: Any
    if isinstance(value, Mapping):
        raw_tags = value
    else:
        text_value = str(value).strip()
        if not text_value:
            return []
        try:
            raw_tags = json.loads(text_value)
        except json.JSONDecodeError:
            return []

    if not isinstance(raw_tags, Mapping):
        return []

    pairs = []
    for raw_key in sorted(raw_tags):
        raw_value = raw_tags[raw_key]
        if raw_value is None:
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        if isinstance(raw_value, (Mapping, list, tuple)):
            label_value = json.dumps(raw_value, ensure_ascii=False, sort_keys=True)
        else:
            label_value = str(raw_value).strip()
        if label_value:
            pairs.append((key, label_value))
    return pairs


def _money(value: Any) -> float:
    numeric = to_number(value)
    return round(float(numeric or 0), 2)


def _today() -> date:
    return datetime.now(UTC).date()


def _date_text(value: Any) -> str | None:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else None


def _observed_days(
    first_value: Any,
    last_value: Any,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
) -> int | None:
    first_seen = _parse_date(first_value)
    last_seen = _parse_date(last_value)
    if first_seen is None or last_seen is None:
        return None
    # If the resource touches either edge of the selected window, we only know a lower
    # bound for how long it existed. Show no duration rather than implying exact uptime.
    if (window_start and first_seen <= window_start) or (window_end and last_seen >= window_end):
        return None
    return max((last_seen - first_seen).days + 1, 1)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
