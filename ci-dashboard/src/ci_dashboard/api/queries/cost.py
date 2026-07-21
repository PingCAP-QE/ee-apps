from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.api.queries.base import CommonFilters, bucket_expr, rate_pct, to_number

COST_STACK_LIMIT = 8
VALID_COST_STACK_GROUPS = frozenset(
    {"repo", "author", "owner", "team", "target_branch", "service", "project", "service_exec_id"}
)
COST_SHARE_LIMIT = 8
VALID_COST_SHARE_DIMENSIONS = frozenset(
    {"owner", "team", "service", "project", "service_exec_id", "region"}
)
LOW_REGION_SHARE_THRESHOLD_PCT = 1.0
UNMATCHED_RESOURCE_LIMIT = 20
UNMATCHED_RESOURCE_MAX_WINDOW_DAYS = 31
ENGINEERING_GROUP_NAME = "Engineering Group"
COST_DATA_LAG_DAYS = 4
FORECAST_WINDOW_DAYS = 14
BUDGET_FALLBACK_MAX_DAYS = 31
UNALLOCATED_GKE_NAMESPACE_BUCKETS = (
    "kube:unallocated",
    "kube:system-overhead",
    "goog-k8s-unsupported-sku",
    "goog-k8s-unknown",
)


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


def get_cost_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        bucket = bucket_expr(connection, "c.usage_date", filters.granularity)
        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  SUM(c.net_cost) AS net_cost,
                  SUM(c.effective_cost) AS effective_cost,
                  SUM({list_cost_expr}) AS list_cost
                FROM cost_attribution_daily c
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
                SELECT
                  SUM({list_cost_expr}) AS total_resource_cost,
                  SUM(CASE WHEN c.attribution_status = 'matched' THEN {list_cost_expr} ELSE 0 END) AS matched_resource_cost
                FROM cost_attribution_daily c
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
            "budget_targets": budget_targets,
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
) -> dict[str, Any]:
    if group_by not in VALID_COST_STACK_GROUPS:
        group_by = "repo"

    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        bucket = bucket_expr(connection, "c.usage_date", filters.granularity)
        dimension = _cost_stack_dimension(connection, group_by)
        list_cost_expr = _billing_report_list_cost_expr("c")
        top_rows = connection.execute(
            text(
                f"""
                SELECT
                  {dimension["expr"]} AS dimension_name,
                  SUM({list_cost_expr}) AS list_cost
                FROM {dimension["from_clause"]}
                WHERE {where_clause}
                GROUP BY dimension_name
                ORDER BY list_cost DESC, dimension_name
                LIMIT :limit
                """
            ),
            {**params, **dimension["params"], "limit": COST_STACK_LIMIT},
        ).mappings()
        top_dimensions = [str(row["dimension_name"] or dimension["empty_label"]) for row in top_rows]
        if not top_dimensions:
            return {
                "series": [],
                "items": [],
                "meta": {**filters.meta(), "limit": COST_STACK_LIMIT, "group_by": group_by},
            }

        dimension_conditions = []
        dimension_params: dict[str, Any] = {}
        for index, dimension_name in enumerate(top_dimensions):
            dimension_key = f"dimension_{index}"
            dimension_conditions.append(
                f"{dimension['expr']} = :{dimension_key}"
            )
            dimension_params[dimension_key] = dimension_name

        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  {dimension["expr"]} AS dimension_name,
                  SUM({list_cost_expr}) AS list_cost
                FROM {dimension["from_clause"]}
                WHERE {where_clause}
                  AND ({" OR ".join(dimension_conditions)})
                GROUP BY bucket_start, dimension_name
                ORDER BY bucket_start, dimension_name
                """
            ),
            {**params, **dimension["params"], **dimension_params},
        ).mappings()
        data_rows = [dict(row) for row in rows]

    buckets = _bucket_starts(filters, data_rows)
    values_by_key = {
        _cost_stack_key(group_by, dimension_name, index): {bucket: 0.0 for bucket in buckets}
        for index, dimension_name in enumerate(top_dimensions)
    }
    labels_by_key = {
        _cost_stack_key(group_by, dimension_name, index): dimension_name
        for index, dimension_name in enumerate(top_dimensions)
    }
    key_by_name = {
        dimension_name: _cost_stack_key(group_by, dimension_name, index)
        for index, dimension_name in enumerate(top_dimensions)
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
            }
            for key in values_by_key
        ],
        "meta": {**filters.meta(), "limit": COST_STACK_LIMIT, "group_by": group_by},
    }


def get_cost_share(
    engine: Engine,
    filters: CommonFilters,
    *,
    dimension: str = "owner",
) -> dict[str, Any]:
    if dimension not in VALID_COST_SHARE_DIMENSIONS:
        dimension = "owner"

    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        dimension_config = _cost_share_dimension(connection, dimension)
        list_cost_expr = _billing_report_list_cost_expr("c")
        rows = connection.execute(
            text(
                f"""
                SELECT
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

    meta = {
        **filters.meta(),
        "dimension": dimension,
        "limit": COST_SHARE_LIMIT,
        "total_list_cost": round(total, 2),
    }
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


def get_engineering_group_share(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
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
                "level1": {"items": [], "meta": {**filters.meta(), "group_name": ENGINEERING_GROUP_NAME}},
                "level2": {"items": [], "meta": {**filters.meta(), "group_name": ENGINEERING_GROUP_NAME}},
            }

        level1 = _engineering_share_by_level(connection, filters, root, level=1)
        level2 = _engineering_share_by_level(connection, filters, root, level=2)

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


def get_unmatched_resources(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
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

    with engine.begin() as connection:
        attr_where_clause, attr_params = _build_cost_where(filters, table_alias="c")
        resource_where_clause, resource_params = _build_cost_where(filters, table_alias="r")
        namespace_clause, namespace_params = _build_unallocated_namespace_where("r.namespace")
        org_match = _null_safe_eq(connection, "m.org", "r.org")
        repo_match = _null_safe_eq(connection, "m.repo", "r.repo")
        author_match = _null_safe_eq(connection, "m.author", "r.author")
        resource_list_cost_expr = _billing_report_list_cost_expr("r")
        rows = connection.execute(
            text(
                f"""
                WITH unmatched_dimensions AS (
                  SELECT
                    c.usage_date AS usage_date,
                    c.vendor AS vendor,
                    c.account_id AS account_id,
                    c.org AS org,
                    c.repo AS repo,
                    c.author AS author,
                    MAX(c.attribution_key) AS attribution_key,
                    MAX(c.attribution_source) AS attribution_source,
                    MAX(c.attribution_status) AS attribution_status
                  FROM cost_attribution_daily c
                  WHERE {attr_where_clause}
                    AND c.employee_id IS NULL
                  GROUP BY
                    c.usage_date,
                    c.vendor,
                    c.account_id,
                    c.org,
                    c.repo,
                    c.author
                ),
                unallocated_resource_rows AS (
                  SELECT
                    r.resource_name AS resource_name,
                    r.service_name AS service_name,
                    r.sku_name AS sku_name,
                    r.org AS org_name,
                    r.repo AS repo_name,
                    r.author AS author_name,
                    r.usage_date AS usage_date,
                    r.namespace AS namespace,
                    r.usage_seconds AS usage_seconds,
                    {resource_list_cost_expr} AS list_cost,
                    m.attribution_key AS attribution_key,
                    m.attribution_source AS attribution_source,
                    m.attribution_status AS attribution_status
                  FROM cost_unmatched_resource_daily r
                  -- Keep attribution as the source of truth; resource rows alone are not roster-filtered.
                  JOIN unmatched_dimensions m
                    ON m.usage_date = r.usage_date
                   AND m.vendor = r.vendor
                   AND m.account_id = r.account_id
                   AND {org_match}
                   AND {repo_match}
                   AND {author_match}
                  WHERE {resource_where_clause}
                    AND r.resource_name IS NOT NULL
                    AND r.resource_name <> ''
                    AND ({namespace_clause})
                ),
                unallocated_resources AS (
                  SELECT
                    resource_name,
                    MAX(service_name) AS service_name,
                    MAX(sku_name) AS sku_name,
                    MAX(org_name) AS org_name,
                    MAX(repo_name) AS repo_name,
                    MAX(author_name) AS author_name,
                    MIN(usage_date) AS first_seen_date,
                    MAX(usage_date) AS last_seen_date,
                    GROUP_CONCAT(DISTINCT COALESCE(namespace, '<null>')) AS allocation_buckets,
                    SUM(COALESCE(usage_seconds, 0)) AS usage_seconds,
                    SUM(list_cost) AS list_cost,
                    MAX(attribution_key) AS attribution_key,
                    MAX(attribution_source) AS attribution_source,
                    MAX(attribution_status) AS attribution_status
                  FROM unallocated_resource_rows
                  GROUP BY resource_name
                )
                SELECT
                  u.resource_name AS resource_name,
                  u.service_name AS service_name,
                  u.sku_name AS sku_name,
                  u.org_name AS org_name,
                  u.repo_name AS repo_name,
                  u.author_name AS author_name,
                  u.first_seen_date AS first_seen_date,
                  u.last_seen_date AS last_seen_date,
                  u.attribution_key AS attribution_key,
                  u.attribution_source AS attribution_source,
                  u.attribution_status AS attribution_status,
                  u.allocation_buckets AS allocation_buckets,
                  u.usage_seconds AS usage_seconds,
                  u.list_cost AS list_cost
                FROM unallocated_resources u
                ORDER BY list_cost DESC, u.resource_name
                LIMIT :limit
                """
            ),
            {
                **attr_params,
                **resource_params,
                **namespace_params,
                "limit": UNMATCHED_RESOURCE_LIMIT,
            },
        ).mappings()
        items = [
            {
                "resource_name": str(row["resource_name"]),
                "service_name": str(row["service_name"] or ""),
                "sku_name": str(row["sku_name"] or ""),
                "repo_name": str(row["repo_name"] or ""),
                "labels": _resource_labels(row),
                "allocation_buckets": str(row["allocation_buckets"] or ""),
                "first_seen_date": _date_text(row["first_seen_date"]),
                "last_seen_date": _date_text(row["last_seen_date"]),
                "observed_days": _observed_days(
                    row["first_seen_date"],
                    row["last_seen_date"],
                    window_start=filters.start_date,
                    window_end=filters.end_date,
                ),
                "attribution_source": str(row["attribution_source"] or ""),
                "attribution_status": str(row["attribution_status"] or ""),
                "usage_seconds": round(float(to_number(row["usage_seconds"]) or 0), 2),
                "list_cost": _money(row["list_cost"]),
            }
            for row in rows
        ]

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
            SELECT
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
    list_cost_expr = _billing_report_list_cost_expr("c")
    row = connection.execute(
        text(
            f"""
            SELECT
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
    list_cost_expr = _billing_report_list_cost_expr("c")
    rows = connection.execute(
        text(
            f"""
            SELECT
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
    if group_by == "project":
        return {
            "expr": "COALESCE(NULLIF(c.project, ''), '(no project)')",
            "from_clause": "cost_attribution_daily c",
            "params": {},
            "empty_label": "(no project)",
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
    for key, label in (
        ("org_name", "org"),
        ("repo_name", "repo"),
        ("author_name", "author"),
        ("attribution_key", "key"),
    ):
        value = str(row[key] or "").strip()
        if value:
            pairs.append(f"{label}={value}")
    return ", ".join(pairs)


def _build_unallocated_namespace_where(column: str) -> tuple[str, dict[str, Any]]:
    conditions = [f"{column} IS NULL"]
    params: dict[str, Any] = {}
    for index, bucket in enumerate(UNALLOCATED_GKE_NAMESPACE_BUCKETS):
        key = f"namespace_bucket_{index}"
        conditions.append(f"{column} = :{key}")
        params[key] = bucket
    return " OR ".join(conditions), params


def _money(value: Any) -> float:
    numeric = to_number(value)
    return round(float(numeric or 0), 2)


def _today() -> date:
    return date.today()


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
