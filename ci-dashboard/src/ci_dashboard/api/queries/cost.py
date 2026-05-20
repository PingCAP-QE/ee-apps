from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.api.queries.base import CommonFilters, bucket_expr, rate_pct, to_number

COST_STACK_LIMIT = 8
UNMATCHED_RESOURCE_LIMIT = 20
ENGINEERING_GROUP_NAME = "Engineering Group"
UNALLOCATED_GKE_NAMESPACE_BUCKETS = (
    "kube:unallocated",
    "kube:system-overhead",
    "goog-k8s-unsupported-sku",
    "goog-k8s-unknown",
)


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
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  SUM(c.net_cost) AS net_cost,
                  SUM(c.effective_cost) AS effective_cost,
                  SUM(c.list_cost) AS list_cost
                FROM cost_attribution_daily c
                WHERE {where_clause}
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        coverage_row = connection.execute(
            text(
                f"""
                SELECT
                  SUM(c.list_cost) AS total_resource_cost,
                  SUM(CASE WHEN c.employee_id IS NOT NULL THEN c.list_cost ELSE 0 END) AS matched_resource_cost
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

    buckets = _bucket_starts(filters, data_rows)
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


def get_repo_group_cost_stack(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = _build_cost_where(filters, table_alias="c")
        bucket = bucket_expr(connection, "c.usage_date", filters.granularity)
        top_rows = connection.execute(
            text(
                f"""
                SELECT
                  COALESCE(NULLIF(c.repo, ''), '(no repo)') AS repo_name,
                  SUM(c.list_cost) AS list_cost
                FROM cost_attribution_daily c
                WHERE {where_clause}
                GROUP BY repo_name
                ORDER BY list_cost DESC, repo_name
                LIMIT :limit
                """
            ),
            {**params, "limit": COST_STACK_LIMIT},
        ).mappings()
        top_repos = [str(row["repo_name"]) for row in top_rows]
        if not top_repos:
            return {
                "series": [],
                "items": [],
                "meta": {**filters.meta(), "limit": COST_STACK_LIMIT},
            }

        dimension_conditions = []
        dimension_params: dict[str, Any] = {}
        for index, repo_name in enumerate(top_repos):
            repo_key = f"repo_{index}"
            dimension_conditions.append(
                f"COALESCE(NULLIF(c.repo, ''), '(no repo)') = :{repo_key}"
            )
            dimension_params[repo_key] = repo_name

        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  COALESCE(NULLIF(c.repo, ''), '(no repo)') AS repo_name,
                  SUM(c.list_cost) AS list_cost
                FROM cost_attribution_daily c
                WHERE {where_clause}
                  AND ({" OR ".join(dimension_conditions)})
                GROUP BY bucket_start, repo_name
                ORDER BY bucket_start, repo_name
                """
            ),
            {**params, **dimension_params},
        ).mappings()
        data_rows = [dict(row) for row in rows]

    buckets = _bucket_starts(filters, data_rows)
    values_by_key = {
        _repo_key(repo_name, index): {bucket: 0.0 for bucket in buckets}
        for index, repo_name in enumerate(top_repos)
    }
    labels_by_key = {
        _repo_key(repo_name, index): repo_name
        for index, repo_name in enumerate(top_repos)
    }
    repo_key_by_name = {
        repo_name: _repo_key(repo_name, index)
        for index, repo_name in enumerate(top_repos)
    }
    for row in data_rows:
        repo_name = str(row["repo_name"])
        key = repo_key_by_name[repo_name]
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
        "meta": {**filters.meta(), "limit": COST_STACK_LIMIT},
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


def get_unmatched_resources(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        attr_where_clause, attr_params = _build_cost_where(filters, table_alias="c")
        raw_where_clause, raw_params = _build_cost_where(filters, table_alias="r")
        namespace_clause, namespace_params = _build_unallocated_namespace_where("r.namespace")
        rows = connection.execute(
            text(
                f"""
                WITH unmatched_resources AS (
                  SELECT
                    c.resource_name AS resource_name,
                    MAX(c.attribution_key) AS attribution_key,
                    MAX(c.attribution_source) AS attribution_source,
                    MAX(c.attribution_status) AS attribution_status
                  FROM cost_attribution_daily c
                  WHERE {attr_where_clause}
                    AND c.employee_id IS NULL
                    AND c.resource_name IS NOT NULL
                    AND c.resource_name <> ''
                  GROUP BY c.resource_name
                ),
                unallocated_resources AS (
                  SELECT
                    r.resource_name AS resource_name,
                    MAX(r.service_name) AS service_name,
                    MAX(r.sku_name) AS sku_name,
                    MAX(r.org) AS org_name,
                    MAX(r.repo) AS repo_name,
                    MAX(r.author) AS author_name,
                    MIN(r.usage_date) AS first_seen_date,
                    MAX(r.usage_date) AS last_seen_date,
                    GROUP_CONCAT(DISTINCT COALESCE(r.namespace, '<null>')) AS allocation_buckets,
                    SUM(COALESCE(r.usage_seconds, 0)) AS usage_seconds,
                    SUM(r.list_cost) AS list_cost
                  FROM cost_raw_details r
                  WHERE {raw_where_clause}
                    AND r.resource_name IS NOT NULL
                    AND r.resource_name <> ''
                    AND ({namespace_clause})
                  GROUP BY r.resource_name
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
                  m.attribution_key AS attribution_key,
                  m.attribution_source AS attribution_source,
                  m.attribution_status AS attribution_status,
                  u.allocation_buckets AS allocation_buckets,
                  u.usage_seconds AS usage_seconds,
                  u.list_cost AS list_cost
                FROM unallocated_resources u
                JOIN unmatched_resources m
                  ON m.resource_name = u.resource_name
                ORDER BY list_cost DESC, u.resource_name
                LIMIT :limit
                """
            ),
            {
                **attr_params,
                **raw_params,
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
        "meta": {**filters.meta(), "limit": UNMATCHED_RESOURCE_LIMIT},
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
              SUM(c.list_cost) AS list_cost
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


def _cost_filters(filters: CommonFilters) -> CommonFilters:
    granularity = filters.granularity if filters.granularity in {"week", "month"} else "week"
    return CommonFilters(
        start_date=filters.start_date,
        end_date=filters.end_date,
        granularity=granularity,
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
    return " AND ".join(conditions), params


def _like_prefix_expr(connection: Connection, value_expr: str, prefix_expr: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"{value_expr} LIKE {prefix_expr} || '%'"
    return f"{value_expr} LIKE CONCAT({prefix_expr}, '%')"


def _repo_key(repo_name: str, index: int) -> str:
    if repo_name == "(no repo)":
        return "repo__no_repo"
    return f"repo__{index}"


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
