from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.api.queries.base import (
    CommonFilters,
    branch_expr,
    bucket_expr,
    build_common_where,
    build_multi_value_clause,
    builds_table_expr,
    failure_like_expr,
    filter_complete_week_rows,
    isoformat_utc,
    rate_pct,
    success_expr,
)
from ci_dashboard.jobs.build_url_matcher import build_job_url, normalized_job_path_from_key

MIGRATION_WINDOW_DAYS = 14
MIGRATION_MIN_SUCCESS_RUNS = 5
MIGRATION_IMPROVED_LIMIT = 10
MIGRATION_REGRESSED_LIMIT = 10
CLOUD_POSTURE_PHASES = ("GCP", "TENCENT")
CLOUD_POSTURE_LABELS = {
    "GCP": "GCP builds",
    "TENCENT": "Tencent builds",
}
BUILD_TREND_JOB_RANKING_LIMIT = 10
BUILD_COUNT_BREAKDOWN_LIMIT = 8
MIGRATION_COMPARISON_BUILD_SYSTEM = "JENKINS"
MIGRATION_RUNTIME_HISTORY_START = date(2025, 12, 15)
MIGRATION_FIXED_BASELINE_START = date(2026, 8, 10)
MIGRATION_FIXED_BASELINE_END = date(2026, 8, 24)
MIGRATION_FIXED_RECENT_START = date(2026, 9, 10)
MIGRATION_FIXED_COMPARISON_SCOPES = (
    ("all_repos", "All repos", None),
    ("tidb", "TiDB", "pingcap/tidb"),
    ("ticdc", "TiCDC", "pingcap/ticdc"),
)
REPO_PERFORMANCE_RANKING_REPOS = (
    "pingcap/docs",
    "pingcap/ticdc",
    "pingcap/tidb",
    "pingcap/tiflash",
    "pingcap/tiflow",
    "tidbcloud/cloud-storage-engine",
    "tikv/pd",
    "tikv/tikv",
)


def get_outcome_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        success_where = success_expr("b")
        failure_like_where = failure_like_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  COUNT(*) AS total_count,
                  SUM(CASE WHEN {success_where} THEN 1 ELSE 0 END) AS success_count,
                  SUM(CASE WHEN {failure_like_where} THEN 1 ELSE 0 END) AS failure_count
                FROM {builds_table}
                WHERE {where_clause}
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

        total_points: list[list[Any]] = []
        success_points: list[list[Any]] = []
        failure_points: list[list[Any]] = []
        rate_points: list[list[Any]] = []
        for row in data_rows:
            bucket_start = str(row["bucket_start"])
            total = int(row["total_count"] or 0)
            success = int(row["success_count"] or 0)
            failure = int(row["failure_count"] or 0)
            total_points.append([bucket_start, total])
            success_points.append([bucket_start, success])
            failure_points.append([bucket_start, failure])
            rate_points.append([bucket_start, rate_pct(success, total)])

        summary = connection.execute(
            text(
                f"""
                SELECT
                  COUNT(*) AS total_count,
                  SUM(CASE WHEN {success_where} THEN 1 ELSE 0 END) AS success_count,
                  SUM(CASE WHEN {failure_like_where} THEN 1 ELSE 0 END) AS failure_count
                FROM {builds_table}
                WHERE {where_clause}
                """
            ),
            params,
        ).mappings().one()

        summary_total = int(summary["total_count"] or 0)
        summary_success = int(summary["success_count"] or 0)
        summary_failure = int(summary["failure_count"] or 0)

    return {
        "series": [
            {"key": "total_count", "type": "bar", "axis": "left", "points": total_points},
            {"key": "success_count", "type": "bar", "axis": "left", "points": success_points},
            {"key": "failure_count", "type": "bar", "axis": "left", "points": failure_points},
            {"key": "success_rate_pct", "type": "line", "axis": "right", "points": rate_points},
        ],
        "meta": {
            **filters.meta(),
            "summary": {
                "total_count": summary_total,
                "success_count": summary_success,
                "failure_count": summary_failure,
                "success_rate_pct": rate_pct(summary_success, summary_total),
            },
        },
    }


def get_duration_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        success_where = success_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  AVG(CASE WHEN {success_where} THEN b.queue_wait_seconds END) AS queue_avg_s,
                  AVG(CASE WHEN {success_where} THEN b.run_seconds END) AS run_avg_s,
                  AVG(CASE WHEN {success_where} THEN b.total_seconds END) AS total_avg_s
                FROM {builds_table}
                WHERE {where_clause}
                  AND b.total_seconds IS NOT NULL
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

        queue_points: list[list[Any]] = []
        run_points: list[list[Any]] = []
        total_points: list[list[Any]] = []
        for row in data_rows:
            bucket_start = str(row["bucket_start"])
            queue_points.append([bucket_start, round(float(row["queue_avg_s"] or 0))])
            run_points.append([bucket_start, round(float(row["run_avg_s"] or 0))])
            total_points.append([bucket_start, round(float(row["total_avg_s"] or 0))])

        summary = connection.execute(
            text(
                f"""
                SELECT
                  AVG(CASE WHEN {success_where} THEN b.queue_wait_seconds END) AS queue_avg_s,
                  AVG(CASE WHEN {success_where} THEN b.run_seconds END) AS run_avg_s,
                  AVG(CASE WHEN {success_where} THEN b.total_seconds END) AS total_avg_s
                FROM {builds_table}
                WHERE {where_clause}
                  AND b.total_seconds IS NOT NULL
                """
            ),
            params,
        ).mappings().one()

    return {
        "series": [
            {"key": "queue_avg_s", "type": "line", "points": queue_points},
            {"key": "run_avg_s", "type": "line", "points": run_points},
            {"key": "total_avg_s", "type": "line", "points": total_points},
        ],
        "meta": {
            **filters.meta(),
            "summary": {
                "queue_avg_s": round(float(summary["queue_avg_s"] or 0)),
                "run_avg_s": round(float(summary["run_avg_s"] or 0)),
                "total_avg_s": round(float(summary["total_avg_s"] or 0)),
            },
        },
    }


def get_cloud_comparison(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        success_where = success_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  b.cloud_phase,
                  COUNT(*) AS total_builds,
                  SUM(CASE WHEN {success_where} THEN 1 ELSE 0 END) AS success_count,
                  AVG(CASE WHEN {success_where} THEN b.queue_wait_seconds END) AS queue_avg_s,
                  AVG(CASE WHEN {success_where} THEN b.run_seconds END) AS run_avg_s,
                  AVG(CASE WHEN {success_where} THEN b.total_seconds END) AS total_avg_s
                FROM {builds_table}
                WHERE {where_clause}
                GROUP BY b.cloud_phase
                ORDER BY b.cloud_phase
                """
            ),
            params,
        ).mappings()

        groups = []
        for row in rows:
            total_builds = int(row["total_builds"] or 0)
            success_count = int(row["success_count"] or 0)
            groups.append(
                {
                    "name": row["cloud_phase"],
                    "metrics": {
                        "total_builds": total_builds,
                        "success_rate_pct": rate_pct(success_count, total_builds),
                        "queue_avg_s": round(float(row["queue_avg_s"] or 0)),
                        "run_avg_s": round(float(row["run_avg_s"] or 0)),
                        "total_avg_s": round(float(row["total_avg_s"] or 0)),
                    },
                }
            )

    return {
        "groups": groups,
        "meta": filters.meta(),
    }


def get_cloud_posture_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                  COUNT(*) AS build_count
                FROM {builds_table}
                WHERE {where_clause}
                  AND UPPER(COALESCE(b.cloud_phase, '')) IN ('GCP', 'TENCENT')
                GROUP BY bucket_start, UPPER(COALESCE(b.cloud_phase, ''))
                ORDER BY bucket_start, UPPER(COALESCE(b.cloud_phase, ''))
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

        bucket_counts = {cloud_phase: {} for cloud_phase in CLOUD_POSTURE_PHASES}
        buckets: set[str] = set()
        for row in data_rows:
            bucket_start = str(row["bucket_start"])
            cloud_phase = str(row["cloud_phase"])
            buckets.add(bucket_start)
            if cloud_phase in bucket_counts:
                bucket_counts[cloud_phase][bucket_start] = int(row["build_count"] or 0)

    ordered_buckets = sorted(buckets)
    if not ordered_buckets:
        return {
            "series": [],
            "meta": {
                **filters.meta(),
                "bucket_granularity": filters.granularity,
            },
        }

    return {
        "series": [
            {
                "key": f"{cloud_phase.lower()}_build_count",
                "label": CLOUD_POSTURE_LABELS[cloud_phase],
                "type": "bar",
                "points": [
                    [bucket_start, bucket_counts[cloud_phase].get(bucket_start, 0)]
                    for bucket_start in ordered_buckets
                ],
            }
            for cloud_phase in CLOUD_POSTURE_PHASES
        ],
        "meta": {
            **filters.meta(),
            "bucket_granularity": filters.granularity,
        },
    }


def get_cloud_migration_summary(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        row = connection.execute(
            text(
                f"""
                SELECT
                  SUM(CASE WHEN UPPER(COALESCE(b.cloud_phase, '')) = 'GCP' THEN 1 ELSE 0 END)
                    AS gcp_build_count,
                  SUM(CASE WHEN UPPER(COALESCE(b.cloud_phase, '')) = 'TENCENT' THEN 1 ELSE 0 END)
                    AS tencent_build_count,
                  SUM(
                    CASE WHEN UPPER(COALESCE(b.cloud_phase, '')) = 'GCP'
                      THEN COALESCE(b.total_seconds, 0) ELSE 0 END
                  ) AS gcp_total_duration_s,
                  SUM(
                    CASE WHEN UPPER(COALESCE(b.cloud_phase, '')) = 'TENCENT'
                      THEN COALESCE(b.total_seconds, 0) ELSE 0 END
                  ) AS tencent_total_duration_s
                FROM {builds_table}
                WHERE {where_clause}
                  AND UPPER(COALESCE(b.cloud_phase, '')) IN ('GCP', 'TENCENT')
                """
            ),
            params,
        ).mappings().one()

    gcp_build_count = int(row["gcp_build_count"] or 0)
    tencent_build_count = int(row["tencent_build_count"] or 0)
    gcp_total_duration_s = int(row["gcp_total_duration_s"] or 0)
    tencent_total_duration_s = int(row["tencent_total_duration_s"] or 0)
    total_build_count = gcp_build_count + tencent_build_count
    total_duration_s = gcp_total_duration_s + tencent_total_duration_s

    return {
        "gcp_build_count": gcp_build_count,
        "tencent_build_count": tencent_build_count,
        "total_build_count": total_build_count,
        "tencent_build_share_pct": rate_pct(tencent_build_count, total_build_count),
        "gcp_total_duration_s": gcp_total_duration_s,
        "tencent_total_duration_s": tencent_total_duration_s,
        "total_duration_s": total_duration_s,
        "tencent_duration_share_pct": rate_pct(tencent_total_duration_s, total_duration_s),
        "meta": filters.meta(),
    }


def get_build_count_breakdown_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    return {
        "repo": _get_build_count_dimension_trend(
            engine,
            filters,
            dimension_key="repo",
            dimension_expr="COALESCE(NULLIF(b.repo_full_name, ''), '(unknown repo)')",
            empty_label="(unknown repo)",
        ),
        "branch": _get_build_count_dimension_trend(
            engine,
            filters,
            dimension_key="branch",
            dimension_expr=f"COALESCE(NULLIF({branch_expr('b')}, ''), '(unknown branch)')",
            empty_label="(unknown branch)",
        ),
        "author": _get_build_count_dimension_trend(
            engine,
            filters,
            dimension_key="author",
            dimension_expr="COALESCE(NULLIF(b.author, ''), '(unknown author)')",
            empty_label="(unknown author)",
        ),
        "meta": {
            **filters.meta(),
            "limit": BUILD_COUNT_BREAKDOWN_LIMIT,
        },
    }


def get_repo_performance_rankings(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        repo_clause, repo_params = build_multi_value_clause(
            "b.repo_full_name",
            REPO_PERFORMANCE_RANKING_REPOS,
            bind_prefix="ranking_repo",
        )
        assert repo_clause is not None
        where_clause = f"{where_clause} AND {repo_clause}"
        params.update(repo_params)
        builds_table = builds_table_expr(connection, filters, alias="b")
        success_where = success_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  COALESCE(NULLIF(b.repo_full_name, ''), '(unknown repo)') AS repo_name,
                  COUNT(*) AS total_build_count,
                  SUM(CASE WHEN {success_where} THEN 1 ELSE 0 END) AS success_build_count,
                  AVG(CASE WHEN {success_where} THEN b.run_seconds END) AS success_avg_run_s
                FROM {builds_table}
                WHERE {where_clause}
                GROUP BY COALESCE(NULLIF(b.repo_full_name, ''), '(unknown repo)')
                """
            ),
            params,
        ).mappings()

        items = []
        for row in rows:
            total_build_count = int(row["total_build_count"] or 0)
            success_build_count = int(row["success_build_count"] or 0)
            success_avg_run_s = row["success_avg_run_s"]
            items.append(
                {
                    "name": str(row["repo_name"]),
                    "total_build_count": total_build_count,
                    "success_build_count": success_build_count,
                    "success_rate_pct": rate_pct(success_build_count, total_build_count),
                    "success_avg_run_s": (
                        round(float(success_avg_run_s))
                        if success_avg_run_s is not None
                        else None
                    ),
                }
            )

    slowest_repos = sorted(
        (item for item in items if item["success_avg_run_s"] is not None),
        key=lambda item: (-int(item["success_avg_run_s"]), -item["success_build_count"], item["name"]),
    )
    lowest_success_rate_repos = sorted(
        items,
        key=lambda item: (item["success_rate_pct"], -item["total_build_count"], item["name"]),
    )

    return {
        "avg_success_duration": {
            "items": [
                {
                    **item,
                    "value": item["success_avg_run_s"],
                }
                for item in slowest_repos
            ],
            "meta": {
                **filters.meta(),
                "repo_count": len(slowest_repos),
                "metric": "success_avg_run_s",
                "success_only": True,
            },
        },
        "success_rate": {
            "items": [
                {
                    **item,
                    "value": item["success_rate_pct"],
                }
                for item in lowest_success_rate_repos
            ],
            "meta": {
                **filters.meta(),
                "repo_count": len(lowest_success_rate_repos),
                "metric": "success_rate_pct",
            },
        },
    }


def _get_build_count_dimension_trend(
    engine: Engine,
    filters: CommonFilters,
    *,
    dimension_key: str,
    dimension_expr: str,
    empty_label: str,
) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  {dimension_expr} AS dimension_name,
                  COUNT(*) AS build_count
                FROM {builds_table}
                WHERE {where_clause}
                GROUP BY bucket_start, {dimension_expr}
                ORDER BY bucket_start, build_count DESC, dimension_name ASC
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

    totals: dict[str, int] = {}
    buckets: set[str] = set()
    counts_by_dimension: dict[str, dict[str, int]] = {}
    for row in data_rows:
        bucket_start = str(row["bucket_start"])
        dimension_name = str(row["dimension_name"] or empty_label)
        build_count = int(row["build_count"] or 0)
        buckets.add(bucket_start)
        totals[dimension_name] = totals.get(dimension_name, 0) + build_count
        counts_by_dimension.setdefault(dimension_name, {})[bucket_start] = build_count

    ordered_buckets = sorted(buckets)
    if not ordered_buckets:
        return {
            "items": [],
            "series": [],
            "meta": {
                **filters.meta(),
                "dimension": dimension_key,
                "limit": BUILD_COUNT_BREAKDOWN_LIMIT,
            },
        }

    total_builds = sum(totals.values())
    ordered_dimensions = sorted(
        totals,
        key=lambda name: (-totals[name], name),
    )
    visible_dimensions = ordered_dimensions[:BUILD_COUNT_BREAKDOWN_LIMIT]
    overflow_dimensions = ordered_dimensions[BUILD_COUNT_BREAKDOWN_LIMIT:]

    items = [
        {
            "name": dimension_name,
            "value": totals[dimension_name],
            "share_pct": rate_pct(totals[dimension_name], total_builds),
        }
        for dimension_name in visible_dimensions
    ]
    if overflow_dimensions:
        other_total = sum(totals[dimension_name] for dimension_name in overflow_dimensions)
        items.append(
            {
                "name": "Others",
                "value": other_total,
                "share_pct": rate_pct(other_total, total_builds),
            }
        )

    series = [
        {
            "key": f"{dimension_key}:{dimension_name}",
            "label": dimension_name,
            "type": "bar",
            "points": [
                [bucket_start, counts_by_dimension.get(dimension_name, {}).get(bucket_start, 0)]
                for bucket_start in ordered_buckets
            ],
        }
        for dimension_name in visible_dimensions
    ]
    if overflow_dimensions:
        series.append(
            {
                "key": f"{dimension_key}:Others",
                "label": "Others",
                "type": "bar",
                "points": [
                    [
                        bucket_start,
                        sum(
                            counts_by_dimension.get(dimension_name, {}).get(bucket_start, 0)
                            for dimension_name in overflow_dimensions
                        ),
                    ]
                    for bucket_start in ordered_buckets
                ],
            }
        )

    return {
        "items": items,
        "series": series,
        "meta": {
            **filters.meta(),
            "dimension": dimension_key,
            "limit": BUILD_COUNT_BREAKDOWN_LIMIT,
            "total_builds": total_builds,
        },
    }


def get_longest_avg_success_jobs(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        success_where = success_expr("b")
        normalized_job_path = _normalized_job_path_expr(connection, "b")
        rows = connection.execute(
            text(
                f"""
                WITH scoped_builds AS (
                  SELECT
                    b.job_name,
                    b.start_time,
                    UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                    {normalized_job_path} AS normalized_job_path,
                    CASE WHEN {success_where} THEN 1 ELSE 0 END AS success_flag,
                    b.run_seconds
                  FROM {builds_table}
                  WHERE {where_clause}
                ),
                latest_job_links AS (
                  SELECT
                    s.job_name,
                    s.normalized_job_path,
                    s.cloud_phase,
                    ROW_NUMBER() OVER (
                      PARTITION BY s.job_name
                      ORDER BY s.start_time DESC, s.normalized_job_path ASC
                    ) AS row_num
                  FROM scoped_builds s
                  WHERE s.normalized_job_path IS NOT NULL
                )
                SELECT
                  s.job_name,
                  COUNT(*) AS total_build_count,
                  SUM(s.success_flag) AS success_build_count,
                  AVG(CASE WHEN s.success_flag = 1 THEN s.run_seconds END) AS success_avg_run_s,
                  lj.normalized_job_path,
                  lj.cloud_phase AS link_cloud_phase
                FROM scoped_builds s
                LEFT JOIN latest_job_links lj
                  ON lj.job_name = s.job_name
                 AND lj.row_num = 1
                GROUP BY s.job_name, lj.normalized_job_path, lj.cloud_phase
                HAVING SUM(s.success_flag) > 0
                ORDER BY success_avg_run_s DESC, success_build_count DESC, s.job_name ASC
                LIMIT :limit
                """
            ),
            {
                **params,
                "limit": BUILD_TREND_JOB_RANKING_LIMIT,
            },
        ).mappings()

        items = []
        for row in rows:
            total_build_count = int(row["total_build_count"] or 0)
            success_build_count = int(row["success_build_count"] or 0)
            items.append(
                {
                    "name": str(row["job_name"]),
                    "value": round(float(row["success_avg_run_s"] or 0)),
                    "total_build_count": total_build_count,
                    "success_build_count": success_build_count,
                    "success_rate_pct": rate_pct(success_build_count, total_build_count),
                    "job_url": build_job_url(row["normalized_job_path"], row["link_cloud_phase"]),
                }
            )

    return {
        "items": items,
        "meta": {
            **filters.meta(),
            "limit": BUILD_TREND_JOB_RANKING_LIMIT,
            "metric": "success_avg_run_s",
            "success_only": True,
        },
    }


def get_lowest_success_rate_jobs(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        success_where = success_expr("b")
        normalized_job_path = _normalized_job_path_expr(connection, "b")
        rows = connection.execute(
            text(
                f"""
                WITH scoped_builds AS (
                  SELECT
                    b.job_name,
                    b.start_time,
                    UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                    {normalized_job_path} AS normalized_job_path,
                    CASE WHEN {success_where} THEN 1 ELSE 0 END AS success_flag,
                    b.run_seconds
                  FROM {builds_table}
                  WHERE {where_clause}
                ),
                latest_job_links AS (
                  SELECT
                    s.job_name,
                    s.normalized_job_path,
                    s.cloud_phase,
                    ROW_NUMBER() OVER (
                      PARTITION BY s.job_name
                      ORDER BY s.start_time DESC, s.normalized_job_path ASC
                    ) AS row_num
                  FROM scoped_builds s
                  WHERE s.normalized_job_path IS NOT NULL
                )
                SELECT
                  s.job_name,
                  COUNT(*) AS total_build_count,
                  SUM(s.success_flag) AS success_build_count,
                  AVG(CASE WHEN s.success_flag = 1 THEN s.run_seconds END) AS success_avg_run_s,
                  ROUND((SUM(s.success_flag) * 100.0) / COUNT(*), 2) AS success_rate_pct,
                  lj.normalized_job_path,
                  lj.cloud_phase AS link_cloud_phase
                FROM scoped_builds s
                LEFT JOIN latest_job_links lj
                  ON lj.job_name = s.job_name
                 AND lj.row_num = 1
                GROUP BY s.job_name, lj.normalized_job_path, lj.cloud_phase
                ORDER BY success_rate_pct ASC, total_build_count DESC, s.job_name ASC
                LIMIT :limit
                """
            ),
            {
                **params,
                "limit": BUILD_TREND_JOB_RANKING_LIMIT,
            },
        ).mappings()

        items = []
        for row in rows:
            total_build_count = int(row["total_build_count"] or 0)
            success_build_count = int(row["success_build_count"] or 0)
            items.append(
                {
                    "name": str(row["job_name"]),
                    "value": round(float(row["success_rate_pct"] or 0), 2),
                    "total_build_count": total_build_count,
                    "success_build_count": success_build_count,
                    "success_avg_run_s": round(float(row["success_avg_run_s"] or 0)),
                    "job_url": build_job_url(row["normalized_job_path"], row["link_cloud_phase"]),
                }
            )

    return {
        "items": items,
        "meta": {
            **filters.meta(),
            "limit": BUILD_TREND_JOB_RANKING_LIMIT,
            "metric": "success_rate_pct",
        },
    }


def get_cloud_repo_share(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        builds_table = builds_table_expr(connection, filters, alias="b")
        branch_name = f"COALESCE(NULLIF({branch_expr('b')}, ''), '(unknown branch)')"
        rows = connection.execute(
            text(
                f"""
                SELECT
                  UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                  b.repo_full_name AS repo_name,
                  {branch_name} AS branch_name,
                  COUNT(*) AS build_count
                FROM {builds_table}
                WHERE {where_clause}
                  AND UPPER(COALESCE(b.cloud_phase, '')) IN ('GCP', 'TENCENT')
                GROUP BY UPPER(COALESCE(b.cloud_phase, '')), b.repo_full_name, {branch_name}
                ORDER BY UPPER(COALESCE(b.cloud_phase, '')), build_count DESC, b.repo_full_name, {branch_name}
                """
            ),
            params,
        ).mappings()

        cloud_repo_counts: dict[str, dict[str, dict[str, Any]]] = {"GCP": {}, "TENCENT": {}}
        cloud_totals = {"GCP": 0, "TENCENT": 0}
        for row in rows:
            cloud_phase = str(row["cloud_phase"])
            repo_name = str(row["repo_name"])
            branch = str(row["branch_name"])
            build_count = int(row["build_count"] or 0)
            cloud_totals[cloud_phase] += build_count
            repo_entry = cloud_repo_counts[cloud_phase].setdefault(
                repo_name,
                {
                    "name": repo_name,
                    "value": 0,
                    "_branch_counts": {},
                },
            )
            repo_entry["value"] += build_count
            repo_entry["_branch_counts"][branch] = repo_entry["_branch_counts"].get(branch, 0) + build_count

    clouds = []
    for cloud_phase in ("GCP", "TENCENT"):
        total_builds = cloud_totals[cloud_phase]
        items = []
        for repo_entry in sorted(
            cloud_repo_counts[cloud_phase].values(),
            key=lambda item: (-int(item["value"]), str(item["name"])),
        ):
            branch_counts: dict[str, int] = repo_entry.pop("_branch_counts")
            repo_total = int(repo_entry["value"])
            branches = [
                {
                    "name": branch_name,
                    "value": branch_value,
                    "share_pct": rate_pct(branch_value, repo_total),
                }
                for branch_name, branch_value in sorted(
                    branch_counts.items(),
                    key=lambda item: (-int(item[1]), str(item[0])),
                )
            ]
            items.append(
                {
                    **repo_entry,
                    "share_pct": rate_pct(repo_total, total_builds),
                    "branches": branches,
                }
            )
        clouds.append(
            {
                "cloud_phase": cloud_phase,
                "total_builds": total_builds,
                "items": items,
            }
        )

    return {
        "clouds": clouds,
        "meta": filters.meta(),
    }


def get_migration_runtime_comparison(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    scope_filters = CommonFilters(
        repo=filters.repo,
        branch=filters.branch,
        job_name=filters.job_name,
        cloud_phase=None,
        issue_status=None,
        start_date=None,
        end_date=filters.end_date,
        granularity=filters.granularity,
    )

    with engine.begin() as connection:
        where_clause, params = build_common_where(scope_filters, table_alias="b")
        builds_table = builds_table_expr(connection, scope_filters, alias="b")
        success_where = success_expr("b")
        anchor_end_date = filters.end_date or _find_latest_tencent_success_date(
            connection,
            where_clause,
            params,
            success_where,
            build_system=MIGRATION_COMPARISON_BUILD_SYSTEM,
        )
        if anchor_end_date is None:
            return {
                "improved": [],
                "regressed": [],
                "meta": {
                    **filters.meta(),
                    "anchor_end_date": None,
                    "window_days": MIGRATION_WINDOW_DAYS,
                    "min_success_runs_each_side": MIGRATION_MIN_SUCCESS_RUNS,
                    "build_system": MIGRATION_COMPARISON_BUILD_SYSTEM,
                }
            }

        anchor_end_exclusive = datetime.combine(anchor_end_date + timedelta(days=1), time.min)
        recent_window_start = anchor_end_exclusive - timedelta(days=MIGRATION_WINDOW_DAYS)
        baseline_window_start = _datetime_shift_expr(
            connection,
            "ft.first_tencent_success_at",
            -MIGRATION_WINDOW_DAYS,
        )

        rows = connection.execute(
            text(
                f"""
                WITH scoped_success_builds AS (
                  SELECT
                    b.repo_full_name,
                    b.job_name,
                    b.normalized_build_url,
                    UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                    b.start_time,
                    b.run_seconds
                  FROM {builds_table}
                  WHERE {where_clause}
                    AND {success_where}
                    AND b.run_seconds IS NOT NULL
                    AND b.start_time >= :migration_history_start
                    AND b.start_time < :anchor_end_exclusive
                    AND UPPER(COALESCE(b.cloud_phase, '')) IN ('GCP', 'TENCENT')
                    AND b.build_system = :migration_comparison_build_system
                    AND b.repo_full_name IS NOT NULL
                    AND b.job_name IS NOT NULL
                    AND b.normalized_build_url IS NOT NULL
                ),
                first_tencent AS (
                  SELECT
                    s.repo_full_name,
                    s.job_name,
                    MIN(s.start_time) AS first_tencent_success_at
                  FROM scoped_success_builds s
                  WHERE s.cloud_phase = 'TENCENT'
                  GROUP BY s.repo_full_name, s.job_name
                ),
                recent_tencent AS (
                  SELECT
                    s.repo_full_name,
                    s.job_name,
                    MIN(s.normalized_build_url) AS sample_build_url,
                    COUNT(*) AS tencent_success_count,
                    AVG(s.run_seconds) AS tencent_recent_avg_run_s
                  FROM scoped_success_builds s
                  WHERE s.cloud_phase = 'TENCENT'
                    AND s.start_time >= :recent_window_start
                  GROUP BY s.repo_full_name, s.job_name
                ),
                gcp_baseline AS (
                  SELECT
                    s.repo_full_name,
                    s.job_name,
                    COUNT(*) AS gcp_success_count,
                    AVG(s.run_seconds) AS gcp_baseline_avg_run_s
                  FROM scoped_success_builds s
                  JOIN first_tencent ft
                    ON ft.repo_full_name = s.repo_full_name
                   AND ft.job_name = s.job_name
                  WHERE s.cloud_phase = 'GCP'
                    AND s.start_time >= {baseline_window_start}
                    AND s.start_time < ft.first_tencent_success_at
                  GROUP BY s.repo_full_name, s.job_name
                )
                SELECT
                  ft.repo_full_name,
                  ft.job_name,
                  rt.sample_build_url,
                  ft.first_tencent_success_at,
                  gb.gcp_success_count,
                  rt.tencent_success_count,
                  gb.gcp_baseline_avg_run_s,
                  rt.tencent_recent_avg_run_s,
                  rt.tencent_recent_avg_run_s - gb.gcp_baseline_avg_run_s AS delta_run_s,
                  CASE
                    WHEN gb.gcp_baseline_avg_run_s = 0 THEN 0
                    ELSE ROUND(
                      ((rt.tencent_recent_avg_run_s - gb.gcp_baseline_avg_run_s) * 100.0)
                      / gb.gcp_baseline_avg_run_s,
                      2
                    )
                  END AS delta_pct
                FROM first_tencent ft
                JOIN recent_tencent rt
                  ON rt.repo_full_name = ft.repo_full_name
                 AND rt.job_name = ft.job_name
                JOIN gcp_baseline gb
                  ON gb.repo_full_name = ft.repo_full_name
                 AND gb.job_name = ft.job_name
                WHERE gb.gcp_success_count >= :min_success_runs_each_side
                  AND rt.tencent_success_count >= :min_success_runs_each_side
                ORDER BY delta_run_s ASC, ft.repo_full_name ASC, ft.job_name ASC
                """
            ),
            {
                **params,
                "migration_history_start": MIGRATION_RUNTIME_HISTORY_START,
                "migration_comparison_build_system": MIGRATION_COMPARISON_BUILD_SYSTEM,
                "anchor_end_exclusive": anchor_end_exclusive,
                "recent_window_start": recent_window_start,
                "min_success_runs_each_side": MIGRATION_MIN_SUCCESS_RUNS,
            },
        ).mappings()

        items = []
        for row in rows:
            normalized_job_path = normalized_job_path_from_key(row["sample_build_url"])
            if normalized_job_path is None:
                continue
            items.append(
                {
                    "job_name": str(row["job_name"]),
                    "normalized_job_path": normalized_job_path,
                    "gcp_baseline_avg_run_s": round(float(row["gcp_baseline_avg_run_s"] or 0)),
                    "tencent_recent_avg_run_s": round(float(row["tencent_recent_avg_run_s"] or 0)),
                    "delta_run_s": round(float(row["delta_run_s"] or 0)),
                    "delta_pct": round(float(row["delta_pct"] or 0), 2),
                    "gcp_success_count": int(row["gcp_success_count"] or 0),
                    "tencent_success_count": int(row["tencent_success_count"] or 0),
                    "first_tencent_success_at": _coerce_isoformat_utc(row["first_tencent_success_at"]),
                }
            )

    improved = [
        item for item in items if item["delta_run_s"] < 0
    ][:MIGRATION_IMPROVED_LIMIT]
    regressed = sorted(
        (item for item in items if item["delta_run_s"] > 0),
        key=lambda item: (-item["delta_run_s"], item["normalized_job_path"]),
    )[:MIGRATION_REGRESSED_LIMIT]

    return {
        "improved": improved,
        "regressed": regressed,
        "meta": {
            **filters.meta(),
            "anchor_end_date": anchor_end_date.isoformat(),
            "window_days": MIGRATION_WINDOW_DAYS,
            "min_success_runs_each_side": MIGRATION_MIN_SUCCESS_RUNS,
            "improved_limit": MIGRATION_IMPROVED_LIMIT,
            "regressed_limit": MIGRATION_REGRESSED_LIMIT,
            "comparison_key": "normalized_job_path",
            "build_system": MIGRATION_COMPARISON_BUILD_SYSTEM,
        },
    }


def get_migration_fixed_window_comparison(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        success_where = success_expr("b")
        recent_end_date = filters.end_date or _find_latest_tencent_success_date(
            connection,
            "1=1",
            {},
            success_where,
            build_system=MIGRATION_COMPARISON_BUILD_SYSTEM,
        )

        rows = [
            {
                "scope_key": scope_key,
                "scope_label": scope_label,
                "repo_full_name": repo_full_name,
                **_fetch_matched_migration_window_comparison(
                    connection,
                    repo_full_name=repo_full_name,
                    recent_end_date=recent_end_date,
                ),
            }
            for scope_key, scope_label, repo_full_name in MIGRATION_FIXED_COMPARISON_SCOPES
        ]

    return {
        "rows": rows,
        "meta": {
            **filters.meta(),
            "baseline_start_date": MIGRATION_FIXED_BASELINE_START.isoformat(),
            "baseline_end_date": MIGRATION_FIXED_BASELINE_END.isoformat(),
            "recent_start_date": MIGRATION_FIXED_RECENT_START.isoformat(),
            "recent_end_date": recent_end_date.isoformat() if recent_end_date else None,
            "build_system": MIGRATION_COMPARISON_BUILD_SYSTEM,
            "scopes": [
                {"scope_key": scope_key, "scope_label": scope_label, "repo_full_name": repo_full_name}
                for scope_key, scope_label, repo_full_name in MIGRATION_FIXED_COMPARISON_SCOPES
            ],
            "ignores_repo_filter": True,
            "ignores_branch_filter": True,
            "ignores_job_filter": True,
            "ignores_start_date": True,
            "ignores_bucket": True,
            "ignores_cloud_phase": True,
        },
    }


def _find_latest_tencent_success_date(
    connection,
    where_clause: str,
    params: dict[str, Any],
    success_where: str,
    *,
    build_system: str | None = None,
):
    row = connection.execute(
        text(
            f"""
            SELECT MAX(DATE(b.start_time)) AS anchor_end_date
            FROM {builds_table_expr(connection, CommonFilters(), alias='b')}
            WHERE {where_clause}
              AND {success_where}
              AND UPPER(COALESCE(b.cloud_phase, '')) = 'TENCENT'
              AND (:build_system IS NULL OR b.build_system = :build_system)
            """
        ),
        {**params, "build_system": build_system},
    ).mappings().one()
    if row["anchor_end_date"] is None:
        return None
    value = row["anchor_end_date"]
    if hasattr(value, "isoformat"):
        return value
    return datetime.fromisoformat(str(value)).date()


def _normalized_job_path_expr(connection, table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    base_key = f"NULLIF({prefix}normalized_build_url, '')"

    if connection.dialect.name == "sqlite":
        return f"normalized_job_path_from_key({base_key})"

    trimmed_key = f"TRIM(TRAILING '/' FROM {base_key})"
    last_segment = f"SUBSTRING_INDEX({trimmed_key}, '/', -1)"
    job_path = (
        "CASE "
        f"WHEN {last_segment} REGEXP '^[0-9]+$' "
        f"THEN LEFT({trimmed_key}, CHAR_LENGTH({trimmed_key}) - CHAR_LENGTH({last_segment}) - 1) "
        f"ELSE {trimmed_key} "
        "END"
    )
    return (
        "CASE "
        f"WHEN {base_key} IS NULL THEN NULL "
        f"ELSE CONCAT(NULLIF({job_path}, ''), '/') "
        "END"
    )


def _datetime_shift_expr(connection, column_expr: str, delta_days: int) -> str:
    if connection.dialect.name == "sqlite":
        sign = "+" if delta_days >= 0 else ""
        return f"DATETIME({column_expr}, '{sign}{delta_days} days')"
    return f"DATE_ADD({column_expr}, INTERVAL {delta_days} DAY)"


def _coerce_isoformat_utc(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return isoformat_utc(datetime.fromisoformat(value))
    return isoformat_utc(value)


def _fetch_matched_migration_window_comparison(
    connection,
    *,
    repo_full_name: str | None,
    recent_end_date: date | None,
) -> dict[str, Any]:
    def empty_summary(start_date: date, end_date: date | None) -> dict[str, Any]:
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat() if end_date else None,
            "total_build_count": 0,
            "success_count": 0,
            "success_rate_pct": 0.0,
            "success_avg_total_s": 0,
        }

    if recent_end_date is None or recent_end_date < MIGRATION_FIXED_RECENT_START:
        return {
            "matched_job_count": 0,
            "baseline": empty_summary(MIGRATION_FIXED_BASELINE_START, MIGRATION_FIXED_BASELINE_END),
            "recent_tencent": empty_summary(MIGRATION_FIXED_RECENT_START, recent_end_date),
        }

    filters = CommonFilters(
        repo=repo_full_name,
        start_date=MIGRATION_FIXED_BASELINE_START,
        end_date=recent_end_date,
    )
    builds_table = builds_table_expr(connection, filters, alias="b")
    repo_clause = "" if repo_full_name is None else "AND b.repo_full_name = :repo_full_name"
    success_where = success_expr("b")
    row = connection.execute(
        text(
            f"""
            WITH gcp_jobs AS (
              SELECT
                b.job_name,
                COUNT(*) AS total_build_count,
                SUM(CASE WHEN {success_where} THEN 1 ELSE 0 END) AS success_count,
                AVG(CASE WHEN {success_where} THEN b.total_seconds END) AS success_avg_total_s
              FROM {builds_table}
              WHERE b.start_time >= :gcp_start_time
                AND b.start_time < :gcp_end_time
                AND UPPER(COALESCE(b.cloud_phase, '')) = 'GCP'
                AND b.build_system = :build_system
                AND b.job_name IS NOT NULL
                {repo_clause}
              GROUP BY b.job_name
            ),
            tencent_jobs AS (
              SELECT
                b.job_name,
                COUNT(*) AS total_build_count,
                SUM(CASE WHEN {success_where} THEN 1 ELSE 0 END) AS success_count,
                AVG(CASE WHEN {success_where} THEN b.total_seconds END) AS success_avg_total_s
              FROM {builds_table}
              WHERE b.start_time >= :tencent_start_time
                AND b.start_time < :tencent_end_time
                AND UPPER(COALESCE(b.cloud_phase, '')) = 'TENCENT'
                AND b.build_system = :build_system
                AND b.job_name IS NOT NULL
                {repo_clause}
              GROUP BY b.job_name
            ),
            matched_jobs AS (
              SELECT
                g.total_build_count AS gcp_total_build_count,
                g.success_count AS gcp_success_count,
                g.success_avg_total_s AS gcp_success_avg_total_s,
                t.total_build_count AS tencent_total_build_count,
                t.success_count AS tencent_success_count,
                t.success_avg_total_s AS tencent_success_avg_total_s
              FROM gcp_jobs g
              JOIN tencent_jobs t ON t.job_name = g.job_name
              WHERE g.success_count > 0
                AND t.success_count > 0
                AND g.success_avg_total_s IS NOT NULL
                AND t.success_avg_total_s IS NOT NULL
            )
            SELECT
              COUNT(*) AS matched_job_count,
              SUM(gcp_total_build_count) AS gcp_total_build_count,
              SUM(gcp_success_count) AS gcp_success_count,
              SUM(tencent_success_count * gcp_success_avg_total_s)
                / NULLIF(SUM(tencent_success_count), 0) AS gcp_reweighted_success_avg_total_s,
              SUM(tencent_total_build_count) AS tencent_total_build_count,
              SUM(tencent_success_count) AS tencent_success_count,
              SUM(tencent_success_count * tencent_success_avg_total_s)
                / NULLIF(SUM(tencent_success_count), 0) AS tencent_success_avg_total_s
            FROM matched_jobs
            """
        ),
        {
            "repo_full_name": repo_full_name,
            "build_system": MIGRATION_COMPARISON_BUILD_SYSTEM,
            "gcp_start_time": datetime.combine(MIGRATION_FIXED_BASELINE_START, time.min),
            "gcp_end_time": datetime.combine(MIGRATION_FIXED_BASELINE_END + timedelta(days=1), time.min),
            "tencent_start_time": datetime.combine(MIGRATION_FIXED_RECENT_START, time.min),
            "tencent_end_time": datetime.combine(recent_end_date + timedelta(days=1), time.min),
        },
    ).mappings().one()

    gcp_total_build_count = int(row["gcp_total_build_count"] or 0)
    gcp_success_count = int(row["gcp_success_count"] or 0)
    tencent_total_build_count = int(row["tencent_total_build_count"] or 0)
    tencent_success_count = int(row["tencent_success_count"] or 0)
    return {
        "matched_job_count": int(row["matched_job_count"] or 0),
        "baseline": {
            "start_date": MIGRATION_FIXED_BASELINE_START.isoformat(),
            "end_date": MIGRATION_FIXED_BASELINE_END.isoformat(),
            "total_build_count": gcp_total_build_count,
            "success_count": gcp_success_count,
            "success_rate_pct": rate_pct(gcp_success_count, gcp_total_build_count),
            "success_avg_total_s": round(float(row["gcp_reweighted_success_avg_total_s"] or 0)),
        },
        "recent_tencent": {
            "start_date": MIGRATION_FIXED_RECENT_START.isoformat(),
            "end_date": recent_end_date.isoformat(),
            "total_build_count": tencent_total_build_count,
            "success_count": tencent_success_count,
            "success_rate_pct": rate_pct(tencent_success_count, tencent_total_build_count),
            "success_avg_total_s": round(float(row["tencent_success_avg_total_s"] or 0)),
        },
    }
