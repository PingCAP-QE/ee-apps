from __future__ import annotations

import base64
import binascii
import calendar
import hashlib
import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
COST_DRILLDOWN_CHILD_GROUPS = {
    "team": "owner",
    "cost_driver": "sku",
}
LOW_REGION_SHARE_THRESHOLD_PCT = 1.0
RESOURCE_BREAKDOWN_DEFAULT_PAGE_SIZE = 50
RESOURCE_BREAKDOWN_MAX_PAGE_SIZE = 100
UNMATCHED_RESOURCE_SORTS = frozenset({"list_cost", "duration"})
RESOURCE_BREAKDOWN_SCOPE_DIMENSIONS = frozenset({"team", "project"})
NO_OWNER_LABEL = "(no owner)"
ENGINEERING_GROUP_NAME = "Engineering Group"
COST_DATA_LAG_DAYS = 4
FORECAST_WINDOW_DAYS = 14
BUDGET_FALLBACK_MAX_DAYS = 31
CURRENT_ATTRIBUTION_BASIS = "current_attribution"
COST_ATTRIBUTION_SOURCE_DATE_INDEX = "idx_cost_attribution_source_date_employee"
TIFLASH_COST_SOURCES = frozenset(
    {
        ("gcp", "pingcap-testing-account"),
        ("aws", "946646677266"),
    }
)
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
) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        drilldown = _cost_drilldown_filter(
            connection,
            child_group=None,
            drilldown_group=drilldown_group,
            drilldown_value=drilldown_value,
        )
        from_clause = "cost_attribution_daily c"
        index_hint = _cost_aggregate_read_hint(connection, filters)
        if drilldown:
            from_clause = drilldown["from_clause"]
            where_clause = f"{where_clause} AND {drilldown['condition']}"
            params = {**params, **drilldown["params"]}
        bucket = bucket_expr(connection, "c.usage_date", filters.granularity)
        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
                WITH bucketed AS (
                  SELECT {index_hint}
                    {bucket} AS bucket_start,
                    SUM(c.net_cost) AS net_cost,
                    SUM(c.effective_cost) AS effective_cost,
                    SUM({list_cost_expr}) AS list_cost,
                    SUM(CASE WHEN c.list_cost IS NOT NULL THEN {list_cost_expr} ELSE 0 END) AS total_resource_cost,
                    SUM(CASE WHEN c.list_cost IS NOT NULL AND c.attribution_status = 'matched' THEN {list_cost_expr} ELSE 0 END) AS matched_resource_cost
                  FROM {from_clause}
                  WHERE {where_clause}
                  GROUP BY bucket_start
                )
                SELECT bucket_start,
                  net_cost,
                  effective_cost,
                  list_cost,
                  SUM(total_resource_cost) OVER () AS total_resource_cost,
                  SUM(matched_resource_cost) OVER () AS matched_resource_cost
                FROM bucketed
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
    summary_net_cost = sum(_money(row["net_cost"]) for row in data_rows)
    summary_effective_cost = sum(_money(row["effective_cost"]) for row in data_rows)
    summary_list_cost = sum(_money(row["list_cost"]) for row in data_rows)
    total_resource_cost = _money(data_rows[0]["total_resource_cost"]) if data_rows else 0.0
    matched_resource_cost = _money(data_rows[0]["matched_resource_cost"]) if data_rows else 0.0

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
            "allocation_basis": CURRENT_ATTRIBUTION_BASIS,
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
) -> dict[str, Any]:
    if group_by not in VALID_COST_STACK_GROUPS:
        group_by = "repo"

    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        index_hint = _cost_aggregate_read_hint(connection, filters)
        bucket = bucket_expr(connection, "c.usage_date", filters.granularity)
        dimension = _cost_stack_dimension(connection, group_by)
        dimension_key_expr = _cost_stack_dimension_key_expr(connection, dimension["expr"])
        dimension_label_expr = _cost_stack_dimension_label_expr(connection, dimension["expr"])
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

        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
                SELECT {index_hint}
                  {bucket} AS bucket_start,
                  {dimension_key_expr} AS dimension_key,
                  {dimension_label_expr} AS dimension_name,
                  SUM({list_cost_expr}) AS list_cost
                FROM {dimension["from_clause"]}
                WHERE {where_clause}
                GROUP BY bucket_start, dimension_key
                ORDER BY bucket_start, dimension_key
                """
            ),
            {**params, **dimension["params"]},
        ).mappings()
        data_rows = [dict(row) for row in rows]

    dimension_totals: dict[str, Any] = {}
    labels_by_dimension_key: dict[str, str] = {}
    for row in data_rows:
        dimension_name = str(row["dimension_name"] or dimension["empty_label"])
        dimension_key = str(row["dimension_key"] or dimension_name.lower())
        labels_by_dimension_key[dimension_key] = min(
            labels_by_dimension_key.get(dimension_key, dimension_name),
            dimension_name,
        )
        list_cost = row["list_cost"]
        if dimension_key not in dimension_totals:
            dimension_totals[dimension_key] = list_cost
        elif list_cost is not None:
            dimension_totals[dimension_key] = (
                list_cost
                if dimension_totals[dimension_key] is None
                else dimension_totals[dimension_key] + list_cost
            )

    top_dimensions = sorted(
        dimension_totals,
        key=lambda key: (
            dimension_totals[key] is None,
            -(dimension_totals[key] or 0),
            key,
        ),
    )
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
            ),
        }

    has_others = len(top_dimensions) > COST_STACK_LIMIT
    visible_dimensions = (
        top_dimensions[: COST_STACK_LIMIT - 1] if has_others else top_dimensions
    )
    visible_dimension_keys = set(visible_dimensions)
    if has_others:
        for row in data_rows:
            dimension_key = str(row["dimension_key"] or "")
            if dimension_key not in visible_dimension_keys:
                row["dimension_key"] = COST_STACK_OTHERS_DIMENSION

    buckets = _bucket_starts(filters, data_rows)
    stack_dimensions = [
        *visible_dimensions,
        *([COST_STACK_OTHERS_DIMENSION] if has_others else []),
    ]
    labels_by_dimension_key[COST_STACK_OTHERS_DIMENSION] = "Others"
    others_key = (
        _cost_stack_key(group_by, COST_STACK_OTHERS_DIMENSION, len(stack_dimensions) - 1)
        if has_others
        else None
    )
    stack_keys = {
        dimension_key: _cost_stack_key(
            group_by,
            labels_by_dimension_key[dimension_key]
            if dimension_key != COST_STACK_OTHERS_DIMENSION
            else dimension_key,
            index,
        )
        for index, dimension_key in enumerate(stack_dimensions)
    }
    values_by_key = {
        stack_keys[dimension_key]: {bucket: 0.0 for bucket in buckets}
        for dimension_key in stack_dimensions
    }
    labels_by_key = {
        stack_keys[dimension_key]: labels_by_dimension_key[dimension_key]
        for dimension_key in stack_dimensions
    }
    for row in data_rows:
        dimension_key = str(row["dimension_key"] or "")
        key = stack_keys[dimension_key]
        bucket_start = str(row["bucket_start"])
        values_by_key[key][bucket_start] += float(row["list_cost"] or 0)

    return {
        "series": [
            {
                "key": key,
                "label": labels_by_key[key],
                "type": "bar",
                "points": [[bucket, _money(values_by_key[key].get(bucket))] for bucket in buckets],
            }
            for key in values_by_key
        ],
        "items": [
            {
                "name": labels_by_key[key],
                "value": round(sum(_money(value) for value in values_by_key[key].values()), 2),
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
        ),
    }


def get_cost_share(
    engine: Engine,
    filters: CommonFilters,
    *,
    dimension: str = "owner",
    drilldown_group: str | None = None,
    drilldown_value: str | None = None,
) -> dict[str, Any]:
    if dimension not in VALID_COST_SHARE_DIMENSIONS:
        dimension = "owner"

    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        index_hint = _cost_aggregate_read_hint(connection, filters)
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
        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
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
) -> dict[str, Any]:
    with engine.begin() as connection:
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
                        "allocation_basis": CURRENT_ATTRIBUTION_BASIS,
                    },
                },
                "level2": {
                    "items": [],
                    "meta": {
                        **filters.meta(),
                        "group_name": ENGINEERING_GROUP_NAME,
                        "allocation_basis": CURRENT_ATTRIBUTION_BASIS,
                    },
                },
            }

        where_clause, params = _build_cost_where(filters, table_alias="c")
        index_hint = _cost_aggregate_read_hint(connection, filters)
        list_cost_expr = _billing_report_list_cost_expr("c")
        level1_match = _like_prefix_expr(connection, "c_group.path", "level1_group.path")
        level2_match = _like_prefix_expr(connection, "c_group.path", "level2_group.path")
        rows = connection.execute(
            text(
                f"""
                SELECT {index_hint}
                  level1_group.id AS level1_id,
                  level1_group.name AS level1_name,
                  level2_group.id AS level2_id,
                  level2_group.name AS level2_name,
                  SUM({list_cost_expr}) AS list_cost
                FROM cost_attribution_daily c
                JOIN roster_groups c_group ON c_group.id = c.group_id
                JOIN roster_groups level1_group
                  ON level1_group.is_active = 1
                 AND level1_group.parent_id = :root_id
                 AND {level1_match}
                LEFT JOIN roster_groups level2_group
                  ON level2_group.is_active = 1
                 AND level2_group.parent_id = level1_group.id
                 AND {level2_match}
                WHERE {where_clause}
                  AND c_group.path IS NOT NULL
                  AND c_group.path LIKE :root_path_like
                GROUP BY level1_group.id, level1_group.name, level2_group.id, level2_group.name
                """
            ),
            {
                **params,
                "root_id": root["id"],
                "root_path_like": f"{root['path']}%",
            },
        ).mappings()
        level1_costs: dict[tuple[Any, str], Any] = {}
        level2_costs: dict[tuple[Any, str], Any] = {}
        for row in rows:
            list_cost = row["list_cost"]
            level1_key = (row["level1_id"], str(row["level1_name"]))
            if level1_key not in level1_costs:
                level1_costs[level1_key] = list_cost
            elif list_cost is not None:
                level1_costs[level1_key] = (
                    list_cost
                    if level1_costs[level1_key] is None
                    else level1_costs[level1_key] + list_cost
                )
            if row["level2_id"] is not None:
                level2_key = (row["level2_id"], str(row["level2_name"]))
                if level2_key not in level2_costs:
                    level2_costs[level2_key] = list_cost
                elif list_cost is not None:
                    level2_costs[level2_key] = (
                        list_cost
                        if level2_costs[level2_key] is None
                        else level2_costs[level2_key] + list_cost
                    )

    level1_items = [
        {"name": key[1], "value": _money(level1_costs[key])}
        for key in sorted(
            level1_costs,
            key=lambda key: (level1_costs[key] is None, -(level1_costs[key] or 0), key[1]),
        )
    ]
    level2_items = [
        {"name": key[1], "value": _money(level2_costs[key])}
        for key in sorted(
            level2_costs,
            key=lambda key: (level2_costs[key] is None, -(level2_costs[key] or 0), key[1]),
        )
    ]
    for level, items in ((1, level1_items), (2, level2_items)):
        total = sum(item["value"] for item in items)
        for item in items:
            item["share_pct"] = rate_pct(item["value"], total)
            item["interactive"] = False
        share = {
            "items": items,
            "meta": {
                **filters.meta(),
                "group_name": ENGINEERING_GROUP_NAME,
                "level": level,
                "total_list_cost": round(total, 2),
                "allocation_basis": CURRENT_ATTRIBUTION_BASIS,
            },
        }
        if level == 1:
            level1 = share
        else:
            level2 = share

    return {"level1": level1, "level2": level2}


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


def get_weekly_cost_report(engine: Engine) -> dict[str, Any]:
    today = _today()
    last_week_start = today - timedelta(days=today.weekday() + 7)
    last_week_end = last_week_start + timedelta(days=6)
    previous_week_end = last_week_start - timedelta(days=1)
    previous_week_start = previous_week_end - timedelta(days=6)
    previous_month_end = today.replace(day=1) - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)
    data_start = min(previous_week_start, previous_month_start)
    data_end = max(last_week_end, previous_month_end)
    history_start = last_week_start - timedelta(days=49)
    history_weeks = [
        {
            "start_date": (history_start + timedelta(days=7 * index)).isoformat(),
            "end_date": (history_start + timedelta(days=7 * index + 6)).isoformat(),
        }
        for index in range(8)
    ]
    report: dict[str, Any] = {
        "meta": {
            "calendar_timezone": "UTC",
            "cost_metric": "list_cost",
            "purpose_schema_available": False,
        },
        "last_week": {
            "start_date": last_week_start.isoformat(),
            "end_date": last_week_end.isoformat(),
        },
        "previous_week": {
            "start_date": previous_week_start.isoformat(),
            "end_date": previous_week_end.isoformat(),
        },
        "previous_month": {
            "start_date": previous_month_start.isoformat(),
            "end_date": previous_month_end.isoformat(),
        },
        "summary": {
            "last_week_cost": 0.0,
            "previous_week_cost": 0.0,
            "week_wow_pct": None,
            "previous_month_cost": 0.0,
        },
        "items": [],
        "list_cost_history": {
            "metric": "list_cost",
            "start_date": history_start.isoformat(),
            "end_date": last_week_end.isoformat(),
            "weeks": history_weeks,
            "series": [],
        },
    }

    with engine.begin() as connection:
        if not _table_has_column(connection, "cost_sources", "purpose"):
            return report
        report["meta"]["purpose_schema_available"] = True
        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  s.vendor,
                  s.account_id,
                  s.display_name,
                  TRIM(s.purpose) AS purpose,
                  SUM(
                    CASE WHEN c.usage_date BETWEEN :last_week_start AND :last_week_end
                      THEN COALESCE({list_cost_expr}, 0) ELSE 0 END
                  ) AS last_week_cost,
                  SUM(
                    CASE WHEN c.usage_date BETWEEN :previous_week_start AND :previous_week_end
                      THEN COALESCE({list_cost_expr}, 0) ELSE 0 END
                  ) AS previous_week_cost,
                  SUM(
                    CASE WHEN c.usage_date BETWEEN :previous_month_start AND :previous_month_end
                      THEN COALESCE({list_cost_expr}, 0) ELSE 0 END
                  ) AS previous_month_cost
                FROM cost_sources s
                LEFT JOIN cost_attribution_daily c
                  ON c.vendor = s.vendor
                 AND c.account_id = s.account_id
                 AND c.usage_date BETWEEN :data_start AND :data_end
                WHERE s.is_active = :is_active
                  AND NULLIF(TRIM(s.purpose), '') IS NOT NULL
                GROUP BY s.vendor, s.account_id, s.display_name, s.purpose
                ORDER BY
                  CASE s.vendor
                    WHEN 'aws' THEN 0
                    WHEN 'gcp' THEN 1
                    ELSE 2
                  END,
                  s.account_id
                """
            ),
            {
                "last_week_start": last_week_start,
                "last_week_end": last_week_end,
                "previous_week_start": previous_week_start,
                "previous_week_end": previous_week_end,
                "previous_month_start": previous_month_start,
                "previous_month_end": previous_month_end,
                "data_start": data_start,
                "data_end": data_end,
                "is_active": 1,
            },
        ).mappings()
        items = [
            {
                "cost_source": _cost_source_value(str(row["vendor"]), str(row["account_id"])),
                "vendor": str(row["vendor"]),
                "account_id": str(row["account_id"]),
                "display_name": str(row["display_name"] or ""),
                "purpose": str(row["purpose"] or ""),
                "last_week_cost": _money(row["last_week_cost"]),
                "previous_week_cost": _money(row["previous_week_cost"]),
                "previous_month_cost": _money(row["previous_month_cost"]),
            }
            for row in rows
        ]
        history_rows = connection.execute(
            text(
                f"""
                SELECT
                  s.vendor,
                  s.account_id,
                  c.usage_date,
                  SUM(COALESCE({list_cost_expr}, 0)) AS list_cost
                FROM cost_sources s
                LEFT JOIN cost_attribution_daily c
                  ON c.vendor = s.vendor
                 AND c.account_id = s.account_id
                 AND c.usage_date BETWEEN :history_start AND :last_week_end
                WHERE s.is_active = :is_active
                  AND NULLIF(TRIM(s.purpose), '') IS NOT NULL
                GROUP BY s.vendor, s.account_id, c.usage_date
                """
            ),
            {
                "history_start": history_start,
                "last_week_end": last_week_end,
                "is_active": 1,
            },
        ).mappings()
        history_values = {
            item["cost_source"]: {week["start_date"]: Decimal(0) for week in history_weeks}
            for item in items
        }
        for row in history_rows:
            if row["usage_date"] is None:
                continue
            usage_date = date.fromisoformat(str(row["usage_date"]))
            week_start = usage_date - timedelta(days=usage_date.weekday())
            source = _cost_source_value(str(row["vendor"]), str(row["account_id"]))
            history_values[source][week_start.isoformat()] += Decimal(str(row["list_cost"] or 0))

    history_series = []
    for item in items:
        values = history_values[item["cost_source"]]
        total_list_cost = sum(values.values())
        history_series.append(
            {
                "cost_source": item["cost_source"],
                "vendor": item["vendor"],
                "account_id": item["account_id"],
                "display_name": item["display_name"],
                "purpose": item["purpose"],
                "total_list_cost": _money(total_list_cost),
                "points": [
                    {"week_start": week["start_date"], "list_cost": _money(values[week["start_date"]])}
                    for week in history_weeks
                ],
            }
        )
    history_series.sort(
        key=lambda item: (
            -sum(history_values[item["cost_source"]].values()),
            item["vendor"],
            item["account_id"],
        )
    )

    total_last_week_cost = _money(sum(item["last_week_cost"] for item in items))
    total_previous_week_cost = _money(sum(item["previous_week_cost"] for item in items))
    total_previous_month_cost = _money(sum(item["previous_month_cost"] for item in items))
    for item in items:
        item["week_wow_pct"] = _nullable_rate_pct(
            item["last_week_cost"] - item["previous_week_cost"],
            item["previous_week_cost"],
        )
        item["last_week_share_pct"] = _nullable_rate_pct(
            item["last_week_cost"], total_last_week_cost
        )

    report["summary"] = {
        "last_week_cost": total_last_week_cost,
        "previous_week_cost": total_previous_week_cost,
        "week_wow_pct": _nullable_rate_pct(
            total_last_week_cost - total_previous_week_cost,
            total_previous_week_cost,
        ),
        "previous_month_cost": total_previous_month_cost,
    }
    report["items"] = items
    report["list_cost_history"]["series"] = history_series
    return report


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
    page_size: int = RESOURCE_BREAKDOWN_DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    scope_dimension: str | None = None,
    scope_value: str | None = None,
) -> dict[str, Any]:
    return _get_published_unmatched_resources(
        engine,
        filters,
        owner=owner,
        service_name=service_name,
        sort_by=sort_by,
        page_size=page_size,
        cursor=cursor,
        scope_dimension=scope_dimension,
        scope_value=scope_value,
    )


def _get_published_unmatched_resources(
    engine: Engine,
    filters: CommonFilters,
    *,
    owner: str | None,
    service_name: str | None,
    sort_by: str,
    page_size: int,
    cursor: str | None,
    scope_dimension: str | None,
    scope_value: str | None,
) -> dict[str, Any]:
    """Read only complete resource-serving publications for this request.

    Publication validity is checked before the Top-resource and service reads.
    A partial native rebuild is consequently a harmless 200/pending
    response rather than a partial result or the retired raw-ledger join.
    """
    requested_filters = filters
    if sort_by not in UNMATCHED_RESOURCE_SORTS:
        sort_by = "list_cost"
    if not 1 <= page_size <= RESOURCE_BREAKDOWN_MAX_PAGE_SIZE:
        raise ValueError("page_size must be between 1 and 100")
    if scope_dimension is not None and scope_dimension not in RESOURCE_BREAKDOWN_SCOPE_DIMENSIONS:
        raise ValueError("scope_dimension must be team or project")
    if scope_dimension is not None and not scope_value:
        raise ValueError("scope_value is required when scope_dimension is set")
    cursor_values = _decode_resource_cursor(cursor, sort_by=sort_by)
    basis_key = "native"
    selected_owner = owner or (scope_value if scope_dimension else NO_OWNER_LABEL)
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
                    SELECT p.vendor, p.account_id, p.usage_date, p.source_row_count,
                      CASE WHEN p.source_row_count = 0 THEN 0
                        WHEN EXISTS (
                          SELECT /*+ NO_DECORRELATE() */ 1
                          FROM cost_resource_serving_daily s
                          WHERE s.basis_key = p.basis_key AND s.vendor = p.vendor
                            AND s.account_id = p.account_id AND s.usage_date = p.usage_date
                            AND s.materialization_version = p.active_materialization_version
                          LIMIT 1
                        ) THEN 1 ELSE 0
                      END AS serving_row_count
                    FROM cost_resource_serving_publication p
                    JOIN scoped_sources scope
                      ON scope.vendor = p.vendor AND scope.account_id = p.account_id
                    WHERE p.basis_key = :basis_key
                      AND p.usage_date BETWEEN :start_date AND :end_date
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
                services=[],
                pending_dates=pending_dates,
                detail_list_cost=0.0,
                total_list_cost=0.0,
                resource_data_source="attribution_fallback",
                scope_dimension=scope_dimension,
                scope_value=scope_value,
                page_size=page_size,
            )
        if not has_serving_tables:
            # This only occurs before migration while no active source/date is
            # expected. Never use the historical broad CTE as a compatibility path.
            return _resource_serving_response(
                items=[], filters=filters, requested_filters=requested_filters,
                selected_owner=selected_owner, service_name=service_filter_name, sort_by=sort_by,
                services=[], pending_dates=[],
                detail_list_cost=0.0, total_list_cost=0.0,
                resource_data_source="attribution_fallback",
                scope_dimension=scope_dimension,
                scope_value=scope_value,
                page_size=page_size,
            )
        if scope_dimension == "project" and not _table_has_column(
            connection, "cost_resource_serving_daily", "project"
        ):
            return _resource_serving_response(
                items=[], filters=filters, requested_filters=requested_filters,
                selected_owner=selected_owner, service_name=service_filter_name, sort_by=sort_by,
                services=[],
                pending_dates=sorted({usage_date.isoformat() for _, _, usage_date in expected_windows}),
                detail_list_cost=0.0, total_list_cost=0.0,
                resource_data_source="attribution_fallback",
                scope_dimension=scope_dimension,
                scope_value=scope_value,
                page_size=page_size,
            )

        branch_clause = "AND s.target_branch = :branch" if filters.branch else ""
        source_clause, source_params = _resource_serving_source_clause(sources)
        scope_clause, scope_params = _resource_serving_scope_clause(
            connection,
            owner=owner,
            scope_dimension=scope_dimension,
            scope_value=scope_value,
        )
        params = {
            "basis_key": basis_key,
            "start_date": filters.start_date,
            "end_date": filters.end_date,
            "service_name": service_filter_name,
            **source_params,
            **scope_params,
        }
        if filters.branch:
            params["branch"] = filters.branch
        validity_clause = "s.basis_key = 'native'"
        service_rows = connection.execute(
            text(
                f"""
                SELECT DISTINCT COALESCE(NULLIF(s.service_name, ''), '(no service)') AS service_name
                FROM cost_resource_serving_daily s
                JOIN cost_resource_serving_publication p
                  ON p.basis_key = s.basis_key AND p.vendor = s.vendor AND p.account_id = s.account_id
                 AND p.usage_date = s.usage_date
                 AND p.active_materialization_version = s.materialization_version
                WHERE s.basis_key = :basis_key AND ({scope_clause})
                  AND s.usage_date BETWEEN :start_date AND :end_date
                  AND ({source_clause})
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
            "a.usage_seconds IS NULL ASC, a.usage_seconds DESC, a.list_cost DESC, "
            "a.resource_group_key ASC"
            if sort_by == "duration"
            else "a.list_cost DESC, a.usage_seconds IS NULL ASC, a.usage_seconds DESC, "
            "a.resource_group_key ASC"
        )
        cursor_clause, cursor_params = _resource_cursor_clause(cursor_values, sort_by=sort_by)
        page_rows = tuple(
            connection.execute(
                text(
                    f"""
                    WITH filtered AS (
                      SELECT s.resource_group_key, s.resource_id, s.resource_name, s.service_name,
                        s.representative_labels_json, s.usage_seconds, s.list_cost,
                        s.detail_list_cost, s.fallback_list_cost, s.usage_date, s.resource_key,
                        s.target_branch
                      FROM cost_resource_serving_daily s
                      JOIN cost_resource_serving_publication p
                        ON p.basis_key = s.basis_key AND p.vendor = s.vendor
                       AND p.account_id = s.account_id AND p.usage_date = s.usage_date
                       AND p.active_materialization_version = s.materialization_version
                      WHERE s.basis_key = :basis_key AND ({scope_clause})
                        AND s.usage_date BETWEEN :start_date AND :end_date
                        AND ({source_clause})
                        AND (:service_name IS NULL OR s.service_name = :service_name)
                        AND {validity_clause} {branch_clause}
                    ),
                    ranked AS (
                      SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY resource_group_key
                        ORDER BY ABS(list_cost) DESC, usage_date ASC, resource_key ASC,
                          COALESCE(target_branch, '') ASC,
                          COALESCE(representative_labels_json, '') ASC
                      ) AS label_rank
                      FROM filtered
                    ),
                    aggregated AS (
                      SELECT /*+ STREAM_AGG() */ resource_group_key, MIN(resource_id) AS resource_id,
                        MIN(resource_name) AS resource_name,
                        GROUP_CONCAT(DISTINCT service_name ORDER BY service_name) AS service_name,
                        MAX(CASE WHEN label_rank = 1 THEN representative_labels_json END)
                          AS representative_labels_json,
                        SUM(usage_seconds) AS usage_seconds,
                        SUM(list_cost) AS list_cost,
                        SUM(detail_list_cost) AS detail_list_cost,
                        SUM(fallback_list_cost) AS fallback_list_cost,
                        SUM(SUM(detail_list_cost)) OVER () AS total_detail_list_cost,
                        SUM(SUM(fallback_list_cost)) OVER () AS total_fallback_list_cost,
                        SUM(SUM(list_cost)) OVER () AS total_list_cost
                      FROM ranked
                      GROUP BY resource_group_key
                      HAVING SUM(list_cost) <> 0
                    )
                    SELECT a.resource_group_key, a.resource_id, a.resource_name,
                      a.service_name, a.representative_labels_json, a.usage_seconds,
                      a.list_cost, a.detail_list_cost, a.fallback_list_cost,
                      a.total_detail_list_cost, a.total_fallback_list_cost, a.total_list_cost
                    FROM aggregated a
                    WHERE {cursor_clause}
                    ORDER BY {order_by}
                    LIMIT :limit
                    """
                ),
                {**params, **cursor_params, "limit": page_size + 1},
            ).mappings()
        )
        has_next_page = len(page_rows) > page_size
        rows = page_rows[:page_size]
        if rows:
            detail_list_cost = Decimal(str(to_number(rows[0]["total_detail_list_cost"]) or 0))
            fallback_list_cost = Decimal(str(to_number(rows[0]["total_fallback_list_cost"]) or 0))
            total_list_cost = Decimal(str(to_number(rows[0]["total_list_cost"]) or 0))
        else:
            coverage = connection.execute(
                text(
                    f"""
                    SELECT
                      COALESCE(SUM(s.detail_list_cost), 0) AS detail_list_cost,
                      COALESCE(SUM(s.fallback_list_cost), 0) AS fallback_list_cost,
                      COALESCE(SUM(s.list_cost), 0) AS total_list_cost
                    FROM cost_resource_serving_daily s
                    JOIN cost_resource_serving_publication p
                      ON p.basis_key = s.basis_key AND p.vendor = s.vendor
                     AND p.account_id = s.account_id AND p.usage_date = s.usage_date
                     AND p.active_materialization_version = s.materialization_version
                    WHERE s.basis_key = :basis_key AND ({scope_clause})
                      AND s.usage_date BETWEEN :start_date AND :end_date
                      AND ({source_clause})
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
            usage_seconds = to_number(row["usage_seconds"])
            items.append(
                {
                    "resource_key": str(row["resource_group_key"]),
                    "resource_id": str(row["resource_id"]) if row["resource_id"] else None,
                    "resource_name": str(row["resource_name"] or "(no resource name)"),
                    "service_name": str(row["service_name"] or ""),
                    "sku_name": "",
                    "repo_name": "",
                    "labels": _format_vendor_labels(row["representative_labels_json"]),
                    "allocation_buckets": "",
                    "first_seen_date": "",
                    "last_seen_date": "",
                    "observed_days": 0,
                    "attribution_source": "",
                    "attribution_status": "",
                    "usage_seconds": None if usage_seconds is None else round(float(usage_seconds), 2),
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
        services=services, pending_dates=[],
        detail_list_cost=float(detail_list_cost), total_list_cost=float(total_list_cost),
        resource_data_source=resource_data_source,
        scope_dimension=scope_dimension,
        scope_value=scope_value,
        page_size=page_size,
        next_cursor=(
            _encode_resource_cursor(rows[-1], sort_by=sort_by) if has_next_page and rows else None
        ),
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


def _resource_serving_source_clause(
    sources: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, str]]:
    """Constrain serving reads with literal source pairs so TiDB uses the owner/date index."""
    clauses = []
    params: dict[str, str] = {}
    for index, source in enumerate(sources):
        vendor_key = f"resource_vendor_{index}"
        account_key = f"resource_account_{index}"
        clauses.append(f"(s.vendor = :{vendor_key} AND s.account_id = :{account_key})")
        params[vendor_key] = str(source["vendor"])
        params[account_key] = str(source["account_id"])
    return " OR ".join(clauses) or "1 = 0", params


def _resource_serving_scope_clause(
    connection: Connection,
    *,
    owner: str | None,
    scope_dimension: str | None,
    scope_value: str | None,
) -> tuple[str, dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {}
    if scope_dimension is None or owner is not None:
        owner_value = "" if owner in (None, NO_OWNER_LABEL) else owner
        clauses.append("s.owner_key = :resource_owner_key")
        params["resource_owner_key"] = hashlib.sha256(owner_value.encode("utf-8")).hexdigest()

    if scope_dimension == "project":
        if scope_value == "(no project)":
            clauses.append("(s.project IS NULL OR s.project = '')")
        else:
            clauses.append("s.project = :resource_scope_project")
            params["resource_scope_project"] = scope_value
    elif scope_dimension == "team":
        team_clause, team_params = _resource_serving_team_clause(connection, scope_value or "")
        clauses.append(team_clause)
        params.update(team_params)

    return " AND ".join(clauses) or "1 = 1", params


def _resource_serving_team_clause(
    connection: Connection,
    scope_value: str,
) -> tuple[str, dict[str, int]]:
    if not _table_exists(connection, "roster_groups"):
        return "1 = 0", {}

    target_rows = connection.execute(
        text(
            """
            SELECT target_group.path
            FROM roster_groups root_group
            JOIN roster_groups target_parent
              ON target_parent.is_active = 1 AND target_parent.parent_id = root_group.id
            JOIN roster_groups target_group
              ON target_group.is_active = 1 AND target_group.parent_id = target_parent.id
            WHERE root_group.name = :root_group_name AND root_group.is_active = 1
            """
            + ("AND target_group.name = :resource_scope_team" if scope_value != "(no team)" else "")
        ),
        {
            "root_group_name": ENGINEERING_GROUP_NAME,
            **({"resource_scope_team": scope_value} if scope_value != "(no team)" else {}),
        },
    ).mappings()
    target_paths = tuple(str(row["path"]) for row in target_rows if row["path"])
    if not target_paths:
        return "1 = 0", {}

    group_rows = connection.execute(
        text("SELECT id, path FROM roster_groups WHERE path IS NOT NULL")
    ).mappings()
    group_ids = tuple(
        int(row["id"])
        for row in group_rows
        if row["id"] is not None and any(str(row["path"]).startswith(path) for path in target_paths)
    )
    if scope_value == "(no team)":
        if not group_ids:
            return "1 = 1", {}
        bind_names = [f"resource_scope_group_{index}" for index in range(len(group_ids))]
        return (
            "(s.group_id IS NULL OR s.group_id NOT IN (" + ", ".join(f":{name}" for name in bind_names) + "))",
            dict(zip(bind_names, group_ids, strict=True)),
        )
    if not group_ids:
        return "1 = 0", {}
    bind_names = [f"resource_scope_group_{index}" for index in range(len(group_ids))]
    return (
        "s.group_id IN (" + ", ".join(f":{name}" for name in bind_names) + ")",
        dict(zip(bind_names, group_ids, strict=True)),
    )


def _encode_resource_cursor(row: Mapping[str, Any], *, sort_by: str) -> str:
    list_cost = _cursor_decimal_text(row["list_cost"])
    usage_seconds = (
        None if row["usage_seconds"] is None else _cursor_decimal_text(row["usage_seconds"])
    )
    values = (
        [list_cost, usage_seconds is None, usage_seconds, str(row["resource_group_key"])]
        if sort_by == "list_cost"
        else [usage_seconds is None, usage_seconds, list_cost, str(row["resource_group_key"])]
    )
    return base64.urlsafe_b64encode(json.dumps(values, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_resource_cursor(cursor: str | None, *, sort_by: str) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        encoded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(encoded.encode()))
        if not isinstance(values, list) or len(values) != 4:
            raise ValueError
        if sort_by == "list_cost":
            list_cost, usage_is_null, usage_seconds, resource_group_key = values
        else:
            usage_is_null, usage_seconds, list_cost, resource_group_key = values
        if (
            isinstance(usage_is_null, bool) is False
            or (usage_is_null and usage_seconds is not None)
            or not isinstance(resource_group_key, str)
            or not resource_group_key
        ):
            raise ValueError
        list_cost = _cursor_decimal_text(list_cost)
        usage_seconds = None if usage_is_null else _cursor_decimal_text(usage_seconds)
    except (binascii.Error, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("invalid resource cursor") from None
    return {
        "list_cost": list_cost,
        "usage_is_null": usage_is_null,
        "usage_seconds": usage_seconds,
        "resource_group_key": resource_group_key,
    }


def _cursor_decimal_text(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError
    return format(decimal_value.normalize(), "f")


def _resource_cursor_clause(
    cursor: Mapping[str, Any] | None, *, sort_by: str
) -> tuple[str, dict[str, Any]]:
    if cursor is None:
        return "1=1", {}
    flag = "CASE WHEN a.usage_seconds IS NULL THEN 1 ELSE 0 END"
    list_cost = "CAST(:cursor_list_cost AS DECIMAL(38,9))"
    usage_seconds = "CAST(:cursor_usage_seconds AS DECIMAL(38,9))"
    params = {
        "cursor_list_cost": cursor["list_cost"],
        "cursor_usage_is_null": int(cursor["usage_is_null"]),
        "cursor_usage_seconds": cursor["usage_seconds"],
        "cursor_resource_group_key": cursor["resource_group_key"],
    }
    if sort_by == "duration":
        return (
            f"""(
              {flag} > :cursor_usage_is_null
              OR ({flag} = :cursor_usage_is_null AND :cursor_usage_is_null = 0
                  AND a.usage_seconds < {usage_seconds})
              OR ({flag} = :cursor_usage_is_null
                  AND (:cursor_usage_is_null = 1 OR a.usage_seconds = {usage_seconds})
                  AND a.list_cost < {list_cost})
              OR ({flag} = :cursor_usage_is_null
                  AND (:cursor_usage_is_null = 1 OR a.usage_seconds = {usage_seconds})
                  AND a.list_cost = {list_cost}
                  AND a.resource_group_key > :cursor_resource_group_key)
            )""",
            params,
        )
    return (
        f"""(
          a.list_cost < {list_cost}
          OR (a.list_cost = {list_cost} AND {flag} > :cursor_usage_is_null)
          OR (a.list_cost = {list_cost} AND {flag} = :cursor_usage_is_null
              AND :cursor_usage_is_null = 0 AND a.usage_seconds < {usage_seconds})
          OR (a.list_cost = {list_cost} AND {flag} = :cursor_usage_is_null
              AND (:cursor_usage_is_null = 1 OR a.usage_seconds = {usage_seconds})
              AND a.resource_group_key > :cursor_resource_group_key)
        )""",
        params,
    )


def _resource_serving_window_is_valid(
    row: Mapping[str, Any] | None,
    *,
    basis_key: str,
) -> bool:
    if row is None:
        return False
    if int(row["source_row_count"] or 0) > 0 and int(row["serving_row_count"] or 0) == 0:
        return False
    return basis_key == "native"


def _resource_serving_response(
    *,
    items: list[dict[str, Any]],
    filters: CommonFilters,
    requested_filters: CommonFilters,
    selected_owner: str,
    service_name: str | None,
    sort_by: str,
    services: list[dict[str, str]],
    pending_dates: list[str],
    detail_list_cost: float,
    total_list_cost: float,
    resource_data_source: str,
    scope_dimension: str | None = None,
    scope_value: str | None = None,
    page_size: int = RESOURCE_BREAKDOWN_DEFAULT_PAGE_SIZE,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "items": items,
        "meta": {
            **filters.meta(),
            "requested_start_date": (
                requested_filters.start_date.isoformat() if requested_filters.start_date else None
            ),
            "window_limited": False,
            "limit": page_size,
            "next_cursor": next_cursor,
            "owner": selected_owner,
            "scope_dimension": scope_dimension,
            "scope_value": scope_value,
            "service_name": service_name,
            "sort_by": sort_by,
            "allocation_basis": CURRENT_ATTRIBUTION_BASIS,
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
    ) -> dict[str, Any]:
    where_clause, params = _build_cost_where(filters, table_alias="c")
    index_hint = _cost_aggregate_read_hint(connection, filters)
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
            SELECT {index_hint}
              target_group.name AS group_name,
              SUM({list_cost_expr}) AS list_cost
            FROM cost_attribution_daily c
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
            "allocation_basis": CURRENT_ATTRIBUTION_BASIS,
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
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        **filters.meta(),
        dimension_key: dimension,
        "limit": limit,
        "allocation_basis": CURRENT_ATTRIBUTION_BASIS,
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


def _cost_stack_dimension_key_expr(connection: Connection, dimension_expr: str) -> str:
    return f"LOWER({dimension_expr})"


def _cost_stack_dimension_label_expr(connection: Connection, dimension_expr: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"MIN({dimension_expr} COLLATE BINARY)"
    return f"CONVERT(MIN(BINARY ({dimension_expr})) USING utf8mb4)"


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


def _nullable_rate_pct(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) * 100.0 / float(denominator), 2)


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
