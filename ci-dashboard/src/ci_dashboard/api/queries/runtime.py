from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.api.queries.base import (
    CommonFilters,
    bucket_expr,
    build_common_where,
    builds_table_expr,
    failure_like_expr,
    filter_complete_week_rows,
    timediff_seconds_expr,
    to_number,
)
from ci_dashboard.jobs.build_url_matcher import (
    build_job_url,
    full_job_name_to_normalized_jenkins_job_path,
    normalize_build_url,
)


IMAGE_PULL_FAILURE_PATTERNS = (
    "imagepullbackoff",
    "errimagepull",
    "back-off pulling image",
    "backoff pulling image",
    "failed to pull image",
    "pull access denied",
    "manifest unknown",
    "repository does not exist",
)


def get_runtime_summary(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, capabilities = _build_pod_build_rows_cte(connection, filters)
        row = connection.execute(
            text(
                f"""
                {cte}
                SELECT
                  COUNT(pod_build_rows.build_id) AS linked_build_count,
                  AVG(pod_build_rows.scheduling_wait_s) AS avg_scheduling_wait_s,
                  SUM(CASE WHEN pod_build_rows.final_scheduling_failure_hit = 1 THEN 1 ELSE 0 END)
                    AS final_scheduling_failure_count,
                  AVG(pod_build_rows.pull_image_s) AS avg_pull_image_s,
                  SUM(CASE WHEN pod_build_rows.image_pull_failure_hit = 0
                             AND (pod_build_rows.pull_attempted = 1 OR pod_build_rows.image_pull_failure_hit = 1)
                           THEN 1 ELSE 0 END) * 100.0
                    / NULLIF(SUM(CASE WHEN pod_build_rows.pull_attempted = 1 OR pod_build_rows.image_pull_failure_hit = 1 THEN 1 ELSE 0 END), 0)
                    AS pull_image_success_rate_pct,
                  SUM(CASE WHEN pod_build_rows.scheduling_wait_s IS NOT NULL THEN 1 ELSE 0 END)
                    AS valid_scheduling_sample_count,
                  SUM(CASE WHEN pod_build_rows.pull_image_s IS NOT NULL THEN 1 ELSE 0 END)
                    AS valid_pull_image_sample_count
                FROM build_scope
                LEFT JOIN pod_build_rows
                  ON pod_build_rows.build_id = build_scope.build_id
                """
            ),
            params,
        ).mappings().one()
        coverage = _get_pod_linkage_coverage(connection, filters)

    return {
        "avg_scheduling_wait_s": _round_or_none(row["avg_scheduling_wait_s"]),
        "final_scheduling_failure_count": int(row["final_scheduling_failure_count"] or 0),
        "avg_pull_image_s": _round_or_zero(row["avg_pull_image_s"]),
        "pull_image_success_rate_pct": _round_or_zero(row["pull_image_success_rate_pct"], digits=1),
        "linked_build_count": int(row["linked_build_count"] or 0),
        "valid_scheduling_sample_count": int(row["valid_scheduling_sample_count"] or 0),
        "valid_pull_image_sample_count": int(row["valid_pull_image_sample_count"] or 0),
        "scheduling_wait_supported": capabilities["scheduling_wait_supported"],
        "scheduling_wait_source_column": capabilities["scheduling_wait_source_column"],
        **coverage,
        "meta": filters.meta(),
    }


def get_scheduling_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, capabilities = _build_pod_build_rows_cte(connection, filters)
        bucket = bucket_expr(connection, "pod_build_rows.scheduling_bucket_time", filters.granularity)
        rows = connection.execute(
            text(
                f"""
                {cte}
                SELECT
                  {bucket} AS bucket_start,
                  AVG(scheduling_wait_s) AS scheduling_wait_avg_s,
                  SUM(CASE WHEN final_scheduling_failure_hit = 1 THEN 1 ELSE 0 END)
                    AS final_failure_count,
                  COUNT(*) AS linked_build_count,
                  SUM(CASE WHEN scheduling_wait_s IS NOT NULL THEN 1 ELSE 0 END)
                    AS valid_sample_count
                FROM pod_build_rows
                WHERE scheduling_bucket_time IS NOT NULL
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        if filters.granularity == "week":
            data_rows = filter_complete_week_rows(
                data_rows,
                start_date=filters.start_date,
                end_date=filters.end_date,
            )

    return {
        "series": (
            [
                {
                    "key": "scheduling_wait_avg_s",
                    "label": "Scheduling wait",
                    "type": "bar",
                    "points": _points(data_rows, "scheduling_wait_avg_s"),
                }
            ]
            if capabilities["scheduling_wait_supported"]
            else []
        )
        + [
            {
                "key": "final_scheduling_failure_count",
                "label": "Final scheduling failures",
                "type": "line",
                "axis": "right",
                "points": _points(data_rows, "final_failure_count"),
            },
        ],
        "meta": {
            **filters.meta(),
            "scheduling_wait_supported": capabilities["scheduling_wait_supported"],
            "scheduling_wait_source_column": capabilities["scheduling_wait_source_column"],
            "sample_counts": [
                {
                    "bucket_start": str(row["bucket_start"]),
                    "linked_build_count": int(row["linked_build_count"] or 0),
                    "valid_sample_count": int(row["valid_sample_count"] or 0),
                }
                for row in data_rows
            ],
        },
    }


def get_scheduling_failure_jobs(engine: Engine, filters: CommonFilters, *, limit: int = 10) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, _capabilities = _build_pod_build_rows_cte(connection, filters)
        params["limit"] = limit
        rows = connection.execute(
            text(
                f"""
                {cte}
                SELECT
                  COALESCE(job_name, '(unknown job)') AS name,
                  SUM(CASE WHEN final_scheduling_failure_hit = 1 THEN 1 ELSE 0 END) AS value,
                  COUNT(*) AS linked_build_count,
                  MIN(cloud_phase) AS link_cloud_phase,
                  SUM(CASE WHEN final_scheduling_failure_hit = 1 THEN 1 ELSE 0 END)
                    AS final_failure_count
                FROM pod_build_rows
                GROUP BY name
                HAVING final_failure_count > 0
                ORDER BY value DESC, linked_build_count DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        items = [
            _job_ranking_item(
                row,
                value_key="value",
                extra_keys=("linked_build_count", "final_failure_count"),
            )
            for row in rows
        ]
    return {"items": items, "meta": filters.meta()}


def get_scheduling_slowest_jobs(engine: Engine, filters: CommonFilters, *, limit: int = 10) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, capabilities = _build_pod_build_rows_cte(connection, filters)
        params.update({"limit": limit, "min_samples": 3})
        rows = connection.execute(
            text(
                f"""
                {cte}
                SELECT
                  COALESCE(job_name, '(unknown job)') AS name,
                  AVG(scheduling_wait_s) AS value,
                  COUNT(*) AS linked_build_count,
                  MIN(cloud_phase) AS link_cloud_phase,
                  SUM(CASE WHEN scheduling_wait_s IS NOT NULL THEN 1 ELSE 0 END)
                    AS valid_sample_count
                FROM pod_build_rows
                WHERE scheduling_wait_s IS NOT NULL
                GROUP BY name
                HAVING valid_sample_count >= :min_samples
                ORDER BY value DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        items = [
            _job_ranking_item(
                row,
                value_key="value",
                extra_keys=("linked_build_count", "valid_sample_count"),
            )
            for row in rows
        ]
    return {
        "items": items,
        "meta": {
            **filters.meta(),
            "scheduling_wait_supported": capabilities["scheduling_wait_supported"],
            "scheduling_wait_source_column": capabilities["scheduling_wait_source_column"],
        },
    }


def get_pull_image_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, _capabilities = _build_pod_build_rows_cte(connection, filters)
        bucket = bucket_expr(connection, "pull_image_bucket_time", filters.granularity)
        rows = connection.execute(
            text(
                f"""
                {cte}
                SELECT
                  {bucket} AS bucket_start,
                  AVG(pull_image_s) AS pull_image_avg_s,
                  SUM(CASE WHEN image_pull_failure_hit = 0
                             AND (pull_attempted = 1 OR image_pull_failure_hit = 1)
                           THEN 1 ELSE 0 END) * 100.0
                    / NULLIF(SUM(CASE WHEN pull_attempted = 1 OR image_pull_failure_hit = 1 THEN 1 ELSE 0 END), 0)
                    AS success_rate_pct,
                  COUNT(*) AS linked_build_count,
                  SUM(CASE WHEN pull_image_s IS NOT NULL THEN 1 ELSE 0 END) AS valid_sample_count
                FROM pod_build_rows
                WHERE pull_image_bucket_time IS NOT NULL
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        if filters.granularity == "week":
            data_rows = filter_complete_week_rows(
                data_rows,
                start_date=filters.start_date,
                end_date=filters.end_date,
            )

    return {
        "series": [
            {
                "key": "pull_image_avg_s",
                "label": "Image pull avg",
                "type": "bar",
                "points": _points(data_rows, "pull_image_avg_s"),
            },
            {
                "key": "pull_image_success_rate_pct",
                "label": "Image pull success rate",
                "type": "line",
                "axis": "right",
                "points": _points(data_rows, "success_rate_pct", digits=1),
            },
        ],
        "meta": {
            **filters.meta(),
            "sample_counts": [
                {
                    "bucket_start": str(row["bucket_start"]),
                    "linked_build_count": int(row["linked_build_count"] or 0),
                    "valid_sample_count": int(row["valid_sample_count"] or 0),
                }
                for row in data_rows
            ],
        },
    }


def get_pull_image_failure_jobs(engine: Engine, filters: CommonFilters, *, limit: int = 10) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, _capabilities = _build_pod_build_rows_cte(connection, filters)
        params["limit"] = limit
        rows = connection.execute(
            text(
                f"""
                {cte}
                SELECT
                  COALESCE(job_name, '(unknown job)') AS name,
                  SUM(CASE WHEN image_pull_failure_hit = 1 THEN 1 ELSE 0 END) AS value,
                  COUNT(*) AS linked_build_count,
                  MIN(cloud_phase) AS link_cloud_phase,
                  SUM(CASE WHEN pull_attempted = 1 THEN 1 ELSE 0 END) AS pull_attempted_count,
                  AVG(pull_image_s) AS avg_pull_image_s
                FROM pod_build_rows
                GROUP BY name
                HAVING value > 0
                ORDER BY value DESC, avg_pull_image_s DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        items = [
            _job_ranking_item(
                row,
                value_key="value",
                extra_keys=("linked_build_count", "pull_attempted_count", "avg_pull_image_s"),
            )
            for row in rows
        ]
    return {"items": items, "meta": filters.meta()}


def get_pull_image_slowest_jobs(engine: Engine, filters: CommonFilters, *, limit: int = 10) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, _capabilities = _build_pod_build_rows_cte(connection, filters)
        params.update({"limit": limit, "min_samples": 3})
        rows = connection.execute(
            text(
                f"""
                {cte}
                SELECT
                  COALESCE(job_name, '(unknown job)') AS name,
                  AVG(pull_image_s) AS value,
                  COUNT(*) AS linked_build_count,
                  MIN(cloud_phase) AS link_cloud_phase,
                  SUM(CASE WHEN pull_image_s IS NOT NULL THEN 1 ELSE 0 END) AS valid_sample_count
                FROM pod_build_rows
                WHERE pull_image_s IS NOT NULL
                GROUP BY name
                HAVING valid_sample_count >= :min_samples
                ORDER BY value DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        items = [
            _job_ranking_item(
                row,
                value_key="value",
                extra_keys=("linked_build_count", "valid_sample_count"),
            )
            for row in rows
        ]
    return {"items": items, "meta": filters.meta()}


def get_pull_image_failure_reasons(engine: Engine, filters: CommonFilters, *, limit: int = 10) -> dict[str, Any]:
    with engine.begin() as connection:
        cte, params, _capabilities = _build_pod_build_rows_cte(connection, filters)
        params["limit"] = limit
        image_pull_reason_where = _image_pull_reason_where_clause(filters, params)
        rows = connection.execute(
            text(
                f"""
                {cte},
                image_pull_reason_rows AS (
                  SELECT
                    COALESCE(events.event_reason, 'message_match') AS name,
                    COUNT(DISTINCT pod_join_rows.build_id) AS value
                  FROM pod_join_rows
                  JOIN ci_l1_pod_events events
                    ON events.source_project = pod_join_rows.source_project
                   AND { _null_safe_join('events.namespace_name', 'pod_join_rows.namespace_name') }
                   AND { _null_safe_join('events.pod_uid', 'pod_join_rows.pod_uid') }
                   AND { _null_safe_join('events.pod_name', 'pod_join_rows.pod_name') }
                  WHERE {image_pull_reason_where}
                  GROUP BY name
                )
                SELECT name, value
                FROM image_pull_reason_rows
                ORDER BY value DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        items = [_ranking_item(row, value_key="value") for row in rows]
    return {"items": items, "meta": filters.meta()}


def get_error_l1_share(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        failure_where = failure_like_expr("b")
        classification_scope = _classification_scope_expr("b")
        l1 = _effective_l1_expr("b")
        l2 = _effective_l2_expr("b")
        l1_rows = connection.execute(
            text(
                f"""
                SELECT {l1} AS name, COUNT(*) AS value
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                GROUP BY name
                ORDER BY value DESC
                """
            ),
            params,
        ).mappings()
        l2_rows = connection.execute(
            text(
                f"""
                SELECT {l1} AS l1_name, {l2} AS name, COUNT(*) AS value
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                GROUP BY l1_name, name
                ORDER BY l1_name, value DESC
                """
            ),
            params,
        ).mappings()

    items = _share_items([dict(row) for row in l1_rows])
    l2_details: dict[str, list[dict[str, Any]]] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in l2_rows:
        grouped.setdefault(str(row["l1_name"]), []).append({"name": row["name"], "value": row["value"]})
    for l1_name, rows in grouped.items():
        l2_details[l1_name] = _share_items(rows)
    return {"items": items, "l2_details": l2_details, "meta": filters.meta()}


def get_error_l1_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    categories = ("INFRA", "BUILD", "UT", "IT", "OTHERS")
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        failure_where = failure_like_expr("b")
        classification_scope = _classification_scope_expr("b")
        l1 = _effective_l1_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  {", ".join(f"SUM(CASE WHEN {l1} = '{category}' THEN 1 ELSE 0 END) AS {category.lower()}_count" for category in categories)}
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        if filters.granularity == "week":
            data_rows = filter_complete_week_rows(
                data_rows,
                start_date=filters.start_date,
                end_date=filters.end_date,
            )
    return {
        "series": [
            {
                "key": category,
                "type": "bar",
                "points": _points(data_rows, f"{category.lower()}_count"),
            }
            for category in categories
        ],
        "meta": filters.meta(),
    }


def get_error_l2_trends(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        failure_where = failure_like_expr("b")
        classification_scope = _classification_scope_expr("b")
        l1 = _effective_l1_expr("b")
        l2 = _effective_l2_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  {l1} AS l1_name,
                  {l2} AS l2_name,
                  COUNT(*) AS value
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                GROUP BY bucket_start, l1_name, l2_name
                ORDER BY bucket_start, l1_name, value DESC
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        if filters.granularity == "week":
            data_rows = filter_complete_week_rows(
                data_rows,
                start_date=filters.start_date,
                end_date=filters.end_date,
            )

    values_by_l1_l2: dict[tuple[str, str], dict[str, int]] = {}
    totals_by_l1: dict[str, dict[str, int]] = {}
    buckets_by_l1: dict[str, set[str]] = {}
    for row in data_rows:
        l1_name = str(row["l1_name"])
        l2_name = str(row["l2_name"])
        bucket_start = str(row["bucket_start"])
        value = int(row["value"] or 0)
        buckets_by_l1.setdefault(l1_name, set()).add(bucket_start)
        totals_by_l1.setdefault(l1_name, {})
        totals_by_l1[l1_name][l2_name] = totals_by_l1[l1_name].get(l2_name, 0) + value
        values_by_l1_l2.setdefault((l1_name, l2_name), {})[bucket_start] = value

    items: dict[str, dict[str, Any]] = {}
    for l1_name, totals_by_l2 in totals_by_l1.items():
        top_l2_names = [
            name
            for name, _value in sorted(
                totals_by_l2.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ]
        buckets = sorted(buckets_by_l1.get(l1_name, set()))
        items[l1_name] = {
            "series": [
                {
                    "key": l2_name,
                    "label": l2_name,
                    "type": "bar",
                    "points": [
                        [bucket_start, values_by_l1_l2.get((l1_name, l2_name), {}).get(bucket_start, 0)]
                        for bucket_start in buckets
                    ],
                }
                for l2_name in top_l2_names
            ],
            "meta": {
                **filters.meta(),
                "l1_category": l1_name,
            },
        }
    return {"items": items, "meta": filters.meta()}


def get_infra_l2_share(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    share = get_error_l1_share(engine, filters)
    return {"items": share.get("l2_details", {}).get("INFRA", []), "meta": filters.meta()}


def get_infra_l2_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    categories = (
        "JENKINS",
        "JENKINS_CACHE",
        "JENKINS_GROOVY",
        "K8S",
        "K8S_MEMORY_EVICTION",
        "OOMKILLED",
        "NETWORK",
        "DISK_FULL",
        "STORAGE",
        "EXTERNAL_DEP",
        "UNCLASSIFIED",
    )
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        failure_where = failure_like_expr("b")
        classification_scope = _classification_scope_expr("b")
        l1 = _effective_l1_expr("b")
        l2 = _effective_l2_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  {", ".join(f"SUM(CASE WHEN {l2} = '{category}' THEN 1 ELSE 0 END) AS {category.lower()}_count" for category in categories)}
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                  AND {l1} = 'INFRA'
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        if filters.granularity == "week":
            data_rows = filter_complete_week_rows(
                data_rows,
                start_date=filters.start_date,
                end_date=filters.end_date,
            )
    return {
        "series": [
            {
                "key": category,
                "type": "bar",
                "points": _points(data_rows, f"{category.lower()}_count"),
            }
            for category in categories
        ],
        "meta": filters.meta(),
    }


def get_error_top_jobs(
    engine: Engine,
    filters: CommonFilters,
    *,
    limit: int = 10,
    l1_category: str | None = None,
    l2_subcategory: str | None = None,
) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        params["limit"] = limit
        builds_table = builds_table_expr(connection, filters, alias="b")
        failure_where = failure_like_expr("b")
        classification_scope = _classification_scope_expr("b")
        l1 = _effective_l1_expr("b")
        l2 = _effective_l2_expr("b")
        if l1_category:
            where_clause = f"{where_clause} AND {l1} = :error_l1_category"
            params["error_l1_category"] = l1_category.strip().upper()
        if l2_subcategory:
            where_clause = f"{where_clause} AND {l2} = :error_l2_subcategory"
            params["error_l2_subcategory"] = l2_subcategory.strip().upper()
        rows = connection.execute(
            text(
                f"""
                SELECT
                  COALESCE(b.job_name, '(unknown job)') AS name,
                  COUNT(*) AS value,
                  MIN(b.cloud_phase) AS link_cloud_phase,
                  SUM(CASE WHEN {l1} = 'INFRA' THEN 1 ELSE 0 END) AS infra_count
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                GROUP BY name
                ORDER BY value DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        items = [
            _job_ranking_item(
                row,
                value_key="value",
                extra_keys=("infra_count",),
            )
            for row in rows
        ]
    return {"items": items, "meta": filters.meta()}


def get_classification_coverage(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        failure_where = failure_like_expr("b")
        classification_scope = _classification_scope_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  SUM(CASE WHEN b.error_l1_category IS NULL AND b.revise_error_l1_category IS NULL THEN 1 ELSE 0 END)
                    AS unclassified_count,
                  SUM(CASE WHEN b.error_l1_category IS NOT NULL OR b.revise_error_l1_category IS NOT NULL THEN 1 ELSE 0 END)
                    AS classified_count
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()
        data_rows = [dict(row) for row in rows]
        if filters.granularity == "week":
            data_rows = filter_complete_week_rows(
                data_rows,
                start_date=filters.start_date,
                end_date=filters.end_date,
            )
        revised = connection.execute(
            text(
                f"""
                SELECT
                  SUM(CASE WHEN b.revise_error_l1_category IS NOT NULL THEN 1 ELSE 0 END) AS revised_count,
                  SUM(CASE WHEN b.error_l1_category IS NOT NULL AND b.revise_error_l1_category IS NULL THEN 1 ELSE 0 END)
                    AS machine_only_count
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                """
            ),
            params,
        ).mappings().one()
        summary = connection.execute(
            text(
                f"""
                SELECT
                  COUNT(*) AS total_failure_like_count,
                  SUM(CASE WHEN {classification_scope} THEN 1 ELSE 0 END)
                    AS classification_scope_count,
                  SUM(CASE WHEN NOT ({classification_scope}) THEN 1 ELSE 0 END)
                    AS skipped_no_log_count,
                  SUM(CASE
                    WHEN {classification_scope}
                     AND (b.error_l1_category IS NOT NULL OR b.revise_error_l1_category IS NOT NULL)
                    THEN 1 ELSE 0
                  END) AS classified_count,
                  SUM(CASE
                    WHEN {classification_scope}
                     AND b.error_l1_category IS NULL
                     AND b.revise_error_l1_category IS NULL
                    THEN 1 ELSE 0
                  END) AS unclassified_count,
                  SUM(CASE
                    WHEN {classification_scope}
                     AND b.revise_error_l1_category IS NOT NULL THEN 1 ELSE 0
                  END) AS human_revised_count,
                  SUM(CASE
                    WHEN {classification_scope}
                     AND COALESCE(b.revise_error_l1_category, b.error_l1_category) IS NOT NULL
                     AND COALESCE(b.revise_error_l1_category, b.error_l1_category) <> 'OTHERS'
                    THEN 1 ELSE 0
                  END) AS specific_classified_count,
                  SUM(CASE
                    WHEN {classification_scope}
                     AND b.revise_error_l1_category IS NULL
                     AND b.error_l1_category IS NOT NULL
                     AND b.error_l1_category <> 'OTHERS'
                    THEN 1 ELSE 0
                  END) AS machine_specific_count,
                  SUM(CASE
                    WHEN {classification_scope}
                     AND b.revise_error_l1_category IS NULL
                     AND b.error_l1_category = 'OTHERS'
                    THEN 1 ELSE 0
                  END) AS machine_others_count,
                  SUM(CASE
                    WHEN {classification_scope}
                     AND b.error_l1_category IS NULL
                     AND b.revise_error_l1_category IS NULL
                     AND b.log_gcs_uri IS NOT NULL
                    THEN 1 ELSE 0
                  END) AS pending_analyze_count,
                  SUM(CASE
                    WHEN NOT ({classification_scope})
                     AND b.error_l1_category IS NULL
                     AND b.revise_error_l1_category IS NULL
                     AND b.log_gcs_uri IS NULL
                    THEN 1 ELSE 0
                  END) AS missing_log_count,
                  SUM(CASE
                    WHEN NOT ({classification_scope})
                     AND b.error_l1_category IS NULL
                     AND b.revise_error_l1_category IS NULL
                     AND b.log_gcs_uri IS NULL
                     AND b.build_system = 'PROW_NATIVE'
                    THEN 1 ELSE 0
                  END) AS no_jenkins_log_count,
                  SUM(CASE
                    WHEN NOT ({classification_scope})
                     AND b.error_l1_category IS NULL
                     AND b.revise_error_l1_category IS NULL
                     AND b.log_gcs_uri IS NULL
                     AND (b.build_system IS NULL OR b.build_system <> 'PROW_NATIVE')
                    THEN 1 ELSE 0
                  END) AS missing_jenkins_log_count
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                """
            ),
            params,
        ).mappings().one()
        latest_pod_event = connection.execute(
            text("SELECT MAX(last_event_at) AS latest_pod_event_at FROM ci_l1_pod_lifecycle")
        ).mappings().one()
    return {
        "classified_vs_unclassified_trend": {
            "series": [
                {
                    "key": "classified_count",
                    "label": "Classified",
                    "type": "bar",
                    "points": _points(data_rows, "classified_count"),
                },
                {
                    "key": "unclassified_count",
                    "label": "Unclassified",
                    "type": "bar",
                    "points": _points(data_rows, "unclassified_count"),
                },
            ],
        },
        "machine_vs_revised": {
            "groups": [
                {
                    "name": "classification_source",
                    "values": [
                        int(revised["machine_only_count"] or 0),
                        int(revised["revised_count"] or 0),
                    ],
                }
            ],
            "categories": ["machine_only", "human_revised"],
        },
        "summary": {
            "total_failure_like_count": int(summary["total_failure_like_count"] or 0),
            "classification_scope_count": int(summary["classification_scope_count"] or 0),
            "skipped_no_log_count": int(summary["skipped_no_log_count"] or 0),
            "classified_count": int(summary["classified_count"] or 0),
            "unclassified_count": int(summary["unclassified_count"] or 0),
            "human_revised_count": int(summary["human_revised_count"] or 0),
            "specific_classified_count": int(summary["specific_classified_count"] or 0),
            "machine_specific_count": int(summary["machine_specific_count"] or 0),
            "machine_others_count": int(summary["machine_others_count"] or 0),
            "pending_analyze_count": int(summary["pending_analyze_count"] or 0),
            "missing_log_count": int(summary["missing_log_count"] or 0),
            "no_jenkins_log_count": int(summary["no_jenkins_log_count"] or 0),
            "missing_jenkins_log_count": int(summary["missing_jenkins_log_count"] or 0),
        },
        "latest_pod_event_at": str(latest_pod_event["latest_pod_event_at"]) if latest_pod_event["latest_pod_event_at"] else None,
        "meta": filters.meta(),
    }


def get_error_builds(
    engine: Engine,
    filters: CommonFilters,
    *,
    job_name: str | None,
    l1_category: str | None = None,
    l2_subcategory: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    selected_job_name = (job_name or "").strip()
    if not selected_job_name:
        return {"items": [], "meta": filters.meta()}

    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        params["limit"] = limit
        params["selected_job_name"] = selected_job_name
        builds_table = builds_table_expr(connection, filters, alias="b")
        failure_where = failure_like_expr("b")
        classification_scope = _classification_scope_expr("b")
        l1 = _effective_l1_expr("b")
        l2 = _effective_l2_expr("b")
        where_clause = f"{where_clause} AND b.job_name = :selected_job_name"
        if l1_category:
            where_clause = f"{where_clause} AND {l1} = :error_l1_category"
            params["error_l1_category"] = l1_category.strip().upper()
        if l2_subcategory:
            where_clause = f"{where_clause} AND {l2} = :error_l2_subcategory"
            params["error_l2_subcategory"] = l2_subcategory.strip().upper()
        rows = connection.execute(
            text(
                f"""
                SELECT
                  b.id AS build_row_id,
                  b.build_id,
                  b.url,
                  b.normalized_build_url,
                  b.completion_time,
                  b.start_time
                FROM {builds_table}
                WHERE {where_clause}
                  AND {failure_where}
                  AND {classification_scope}
                ORDER BY
                  CASE WHEN b.completion_time IS NULL THEN 1 ELSE 0 END ASC,
                  b.completion_time DESC,
                  b.start_time DESC,
                  b.id DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        items = [_error_build_item(row) for row in rows]
    return {"items": items, "meta": filters.meta()}


def _build_pod_build_rows_cte(
    connection: Connection,
    filters: CommonFilters,
) -> tuple[str, dict[str, Any], dict[str, bool]]:
    where_clause, params = build_common_where(filters, table_alias="b")
    params["final_scheduling_failure_cutoff_at"] = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    builds_table = builds_table_expr(connection, filters, alias="b")
    pod_created_column = _pod_created_column(connection)
    pod_created_select = f"p.{pod_created_column}" if pod_created_column else "NULL"
    scheduling_wait_s = timediff_seconds_expr(connection, "pc.pod_created_at", "pc.scheduled_at")
    pull_image_s = timediff_seconds_expr(connection, "pc.first_pulling_at", "pc.first_pulled_at")
    resolved_job_name = "COALESCE(NULLIF(pc.build_job_name, ''), NULLIF(pc.pod_job_name, ''))"
    canonical_job_name = _canonical_job_name_expr(
        connection,
        job_expr=resolved_job_name,
        repo_expr="pc.repo_full_name",
    )
    return (
        f"""
        WITH build_scope AS (
          SELECT
            b.id AS build_id,
            b.start_time,
            b.repo_full_name,
            b.job_name AS build_job_name,
            b.cloud_phase,
            b.normalized_build_url,
            b.source_prow_job_id
          FROM {builds_table}
          WHERE {where_clause}
        ),
        pod_candidates AS (
          SELECT
            b.build_id,
            b.start_time,
            b.repo_full_name,
            b.build_job_name,
            b.cloud_phase,
            p.job_name AS pod_job_name,
            p.source_project,
            p.namespace_name,
            p.pod_uid,
            p.pod_name,
            {pod_created_select} AS pod_created_at,
            p.scheduled_at,
            p.last_event_at,
            p.first_pulling_at,
            p.first_pulled_at,
            p.last_failed_scheduling_at,
            p.failed_scheduling_count
          FROM ci_l1_pod_lifecycle p
          JOIN build_scope b
            ON p.normalized_build_url IS NOT NULL
           AND p.normalized_build_url <> ''
           AND b.normalized_build_url = p.normalized_build_url
          UNION ALL
          SELECT
            b.build_id,
            b.start_time,
            b.repo_full_name,
            b.build_job_name,
            b.cloud_phase,
            p.job_name AS pod_job_name,
            p.source_project,
            p.namespace_name,
            p.pod_uid,
            p.pod_name,
            {pod_created_select} AS pod_created_at,
            p.scheduled_at,
            p.last_event_at,
            p.first_pulling_at,
            p.first_pulled_at,
            p.last_failed_scheduling_at,
            p.failed_scheduling_count
          FROM ci_l1_pod_lifecycle p
          JOIN build_scope b
            ON (p.normalized_build_url IS NULL OR p.normalized_build_url = '')
           AND p.source_prow_job_id IS NOT NULL
           AND p.source_prow_job_id <> ''
           AND b.source_prow_job_id = p.source_prow_job_id
        ),
        pod_join_rows AS (
          SELECT
            pc.build_id,
            pc.start_time,
            pc.repo_full_name,
            {canonical_job_name} AS job_name,
            pc.cloud_phase,
            pc.source_project,
            pc.namespace_name,
            pc.pod_uid,
            pc.pod_name,
            pc.pod_created_at,
            pc.scheduled_at,
            pc.last_event_at,
            pc.last_failed_scheduling_at,
            pc.first_pulling_at,
            pc.first_pulled_at,
            CASE
              WHEN pc.pod_created_at IS NOT NULL
               AND pc.scheduled_at IS NOT NULL
               AND pc.scheduled_at >= pc.pod_created_at
              THEN {scheduling_wait_s}
            END AS scheduling_wait_s,
            CASE
              WHEN pc.first_pulling_at IS NOT NULL
               AND pc.first_pulled_at IS NOT NULL
               AND pc.first_pulled_at >= pc.first_pulling_at
              THEN {pull_image_s}
            END AS pull_image_s,
            CASE WHEN pc.failed_scheduling_count > 0 THEN 1 ELSE 0 END AS failed_scheduling_hit,
            CASE
              WHEN pc.failed_scheduling_count > 0
               AND pc.scheduled_at IS NULL
               AND pc.last_event_at IS NOT NULL
               AND pc.last_event_at < :final_scheduling_failure_cutoff_at
              THEN 1 ELSE 0
            END AS final_scheduling_failure_hit,
            CASE WHEN pc.first_pulling_at IS NOT NULL THEN 1 ELSE 0 END AS pull_attempted,
            CASE
              WHEN pc.first_pulling_at IS NOT NULL AND pc.first_pulled_at IS NULL
              THEN 1 ELSE 0
            END AS image_pull_failure_hit
          FROM pod_candidates pc
        ),
        pod_build_rows AS (
          SELECT
            build_id,
            MIN(start_time) AS start_time,
            MIN(repo_full_name) AS repo_full_name,
            MIN(job_name) AS job_name,
            MIN(cloud_phase) AS cloud_phase,
            MIN(COALESCE(pod_created_at, scheduled_at, last_failed_scheduling_at)) AS scheduling_bucket_time,
            MIN(COALESCE(first_pulling_at, first_pulled_at)) AS pull_image_bucket_time,
            COUNT(*) AS linked_pod_count,
            MAX(scheduling_wait_s) AS scheduling_wait_s,
            MAX(pull_image_s) AS pull_image_s,
            MAX(failed_scheduling_hit) AS failed_scheduling_hit,
            MAX(final_scheduling_failure_hit) AS final_scheduling_failure_hit,
            MAX(pull_attempted) AS pull_attempted,
            MAX(image_pull_failure_hit) AS image_pull_failure_hit
          FROM pod_join_rows
          GROUP BY build_id
        )
        """,
        params,
        {
            "scheduling_wait_supported": pod_created_column is not None,
            "scheduling_wait_source_column": pod_created_column,
        },
    )


def _image_pull_reason_where_clause(filters: CommonFilters, params: dict[str, Any]) -> str:
    conditions = [
        "events.event_reason IN ('Failed', 'BackOff', 'ErrImagePull', 'ImagePullBackOff')",
        f"({_image_pull_failure_condition('events')})",
    ]
    if filters.start_date:
        params["pod_event_time_from"] = datetime.combine(
            filters.start_date - timedelta(days=1),
            time.min,
        )
        conditions.append("events.event_timestamp >= :pod_event_time_from")
    if filters.end_date:
        params["pod_event_time_to"] = datetime.combine(
            filters.end_date + timedelta(days=2),
            time.min,
        )
        conditions.append("events.event_timestamp < :pod_event_time_to")
    return " AND ".join(conditions)


def _get_pod_linkage_coverage(connection: Connection, filters: CommonFilters) -> dict[str, Any]:
    cte, cte_params, _capabilities = _build_pod_build_rows_cte(connection, filters)
    row = connection.execute(
        text(
            f"""
            {cte},
            build_scope_count AS (
              SELECT COUNT(*) AS total_build_count FROM build_scope
            )
            SELECT
              build_scope_count.total_build_count,
              COUNT(DISTINCT pod_build_rows.build_id) AS builds_with_pod_count,
              COALESCE(SUM(pod_build_rows.linked_pod_count), 0) AS linked_pod_row_count
            FROM build_scope_count
            LEFT JOIN pod_build_rows ON 1=1
            GROUP BY build_scope_count.total_build_count
            """
        ),
        cte_params,
    ).mappings().one()
    total = int(row["total_build_count"] or 0)
    linked = int(row["builds_with_pod_count"] or 0)
    return {
        "total_build_count": total,
        "builds_with_pod_count": linked,
        "linked_pod_row_count": int(row["linked_pod_row_count"] or 0),
        "pod_linkage_coverage_pct": round((linked * 100.0 / total), 1) if total else 0,
    }


def _effective_l1_expr(alias: str) -> str:
    return f"COALESCE({alias}.revise_error_l1_category, {alias}.error_l1_category, 'OTHERS')"


def _effective_l2_expr(alias: str) -> str:
    return f"COALESCE({alias}.revise_error_l2_subcategory, {alias}.error_l2_subcategory, 'UNCLASSIFIED')"


def _classification_scope_expr(alias: str) -> str:
    return (
        f"({alias}.log_gcs_uri IS NOT NULL "
        f"OR {alias}.error_l1_category IS NOT NULL "
        f"OR {alias}.revise_error_l1_category IS NOT NULL)"
    )


def _pod_created_column(connection: Connection) -> str | None:
    if _table_has_column(connection, "ci_l1_pod_lifecycle", "pod_created_at"):
        return "pod_created_at"
    return None


def _image_pull_failure_condition(alias: str) -> str:
    return (
        f"{alias}.event_reason IN ('ImagePullBackOff', 'ErrImagePull') "
        f"OR {_image_pull_failure_message_condition(alias)}"
    )


def _image_pull_failure_message_condition(alias: str) -> str:
    lowered = f"LOWER(COALESCE({alias}.event_message, ''))"
    return " OR ".join(f"{lowered} LIKE '%{pattern}%'" for pattern in IMAGE_PULL_FAILURE_PATTERNS)


def _null_safe_join(left: str, right: str) -> str:
    return f"({left} = {right} OR ({left} IS NULL AND {right} IS NULL))"


def _points(rows: list[dict[str, Any]], key: str, *, digits: int = 0) -> list[list[Any]]:
    points: list[list[Any]] = []
    for row in rows:
        value = row.get(key)
        if value is None:
            points.append([str(row["bucket_start"]), None])
        else:
            points.append([str(row["bucket_start"]), round(float(value), digits)])
    return points


def _share_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = sum(int(row.get("value") or 0) for row in rows) or 1
    return [
        {
            "name": str(row["name"]),
            "value": int(row.get("value") or 0),
            "share_pct": round((int(row.get("value") or 0) * 100.0) / total, 1),
        }
        for row in rows
    ]


def _ranking_item(row: Any, *, value_key: str, extra_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    item = {
        "name": str(row["name"]),
        "value": to_number(row[value_key]) or 0,
    }
    for key in extra_keys:
        item[key] = to_number(row[key])
    return item


def _job_ranking_item(row: Any, *, value_key: str, extra_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    item = _ranking_item(row, value_key=value_key, extra_keys=extra_keys)
    item["job_url"] = build_job_url(
        full_job_name_to_normalized_jenkins_job_path(item["name"]),
        row.get("link_cloud_phase"),
    )
    return item


def _error_build_item(row: Any) -> dict[str, Any]:
    build_url = normalize_build_url(row.get("normalized_build_url")) or normalize_build_url(row.get("url"))
    build_number_from_url = _build_number_from_url(build_url)
    raw_build_number = str(row.get("build_id") or "").strip()
    if build_number_from_url:
        build_number = build_number_from_url
    elif raw_build_number:
        # Fallback only when URL does not contain a Jenkins numeric run id.
        build_number = raw_build_number
    else:
        build_number = str(row.get("build_row_id"))
    return {
        "name": build_number,
        "build_number": build_number,
        "build_url": build_url,
    }


def _build_number_from_url(url: str | None) -> str | None:
    if not url:
        return None
    stripped = str(url).rstrip("/")
    if not stripped:
        return None
    last_segment = stripped.split("/")[-1]
    return last_segment if last_segment.isdigit() else None


def _round_or_zero(value: Any, *, digits: int = 0) -> int | float:
    if value is None:
        return 0
    rounded = round(float(value), digits)
    return int(rounded) if digits == 0 else rounded


def _round_or_none(value: Any, *, digits: int = 0) -> int | float | None:
    if value is None:
        return None
    rounded = round(float(value), digits)
    return int(rounded) if digits == 0 else rounded


def _table_has_column(connection: Connection, table_name: str, column_name: str) -> bool:
    columns = inspect(connection).get_columns(table_name)
    return any(column.get("name") == column_name for column in columns)


def _canonical_job_name_expr(connection: Connection, *, job_expr: str, repo_expr: str) -> str:
    full_job_expr = _concat_full_job_name_expr(connection, repo_expr, job_expr)
    return (
        "CASE "
        f"WHEN {job_expr} IS NULL OR {job_expr} = '' THEN {job_expr} "
        f"WHEN {job_expr} LIKE '%/%' THEN {job_expr} "
        f"WHEN {repo_expr} IS NOT NULL AND {repo_expr} <> '' AND {job_expr} NOT LIKE '% %' THEN {full_job_expr} "
        f"ELSE {job_expr} END"
    )


def _concat_full_job_name_expr(connection: Connection, repo_expr: str, job_expr: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"({repo_expr} || '/' || {job_expr})"
    return f"CONCAT({repo_expr}, '/', {job_expr})"
