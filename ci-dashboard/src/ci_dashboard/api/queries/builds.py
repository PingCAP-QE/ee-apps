from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.api.queries.base import (
    CommonFilters,
    branch_expr,
    bucket_expr,
    build_common_where,
    failure_like_expr,
    isoformat_utc,
    rate_pct,
    success_expr,
)
from ci_dashboard.jobs.build_url_matcher import build_job_url

MIGRATION_WINDOW_DAYS = 14
MIGRATION_MIN_SUCCESS_RUNS = 5
MIGRATION_IMPROVED_LIMIT = 10
MIGRATION_REGRESSED_LIMIT = 10
BUILD_TREND_JOB_RANKING_LIMIT = 10


def get_outcome_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
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
                FROM ci_l1_builds b
                WHERE {where_clause}
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()

        total_points: list[list[Any]] = []
        success_points: list[list[Any]] = []
        failure_points: list[list[Any]] = []
        rate_points: list[list[Any]] = []
        for row in rows:
            bucket_start = str(row["bucket_start"])
            total = int(row["total_count"] or 0)
            success = int(row["success_count"] or 0)
            failure = int(row["failure_count"] or 0)
            total_points.append([bucket_start, total])
            success_points.append([bucket_start, success])
            failure_points.append([bucket_start, failure])
            rate_points.append([bucket_start, rate_pct(success, total)])

    return {
        "series": [
            {"key": "total_count", "type": "bar", "axis": "left", "points": total_points},
            {"key": "success_count", "type": "bar", "axis": "left", "points": success_points},
            {"key": "failure_count", "type": "bar", "axis": "left", "points": failure_points},
            {"key": "success_rate_pct", "type": "line", "axis": "right", "points": rate_points},
        ],
        "meta": filters.meta(),
    }


def get_duration_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
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
                FROM ci_l1_builds b
                WHERE {where_clause}
                  AND b.total_seconds IS NOT NULL
                GROUP BY bucket_start
                ORDER BY bucket_start
                """
            ),
            params,
        ).mappings()

        queue_points: list[list[Any]] = []
        run_points: list[list[Any]] = []
        total_points: list[list[Any]] = []
        for row in rows:
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
                FROM ci_l1_builds b
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
                FROM ci_l1_builds b
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
        bucket = bucket_expr(connection, "b.start_time", "week")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                  COUNT(*) AS build_count
                FROM ci_l1_builds b
                WHERE {where_clause}
                  AND UPPER(COALESCE(b.cloud_phase, '')) IN ('GCP', 'IDC')
                GROUP BY bucket_start, UPPER(COALESCE(b.cloud_phase, ''))
                ORDER BY bucket_start, UPPER(COALESCE(b.cloud_phase, ''))
                """
            ),
            params,
        ).mappings()

        weekly_counts = {"GCP": {}, "IDC": {}}
        buckets: set[str] = set()
        for row in rows:
            bucket_start = str(row["bucket_start"])
            cloud_phase = str(row["cloud_phase"])
            buckets.add(bucket_start)
            if cloud_phase in weekly_counts:
                weekly_counts[cloud_phase][bucket_start] = int(row["build_count"] or 0)

    ordered_buckets = sorted(buckets)
    if not ordered_buckets:
        return {
            "series": [],
            "meta": {
                **filters.meta(),
                "bucket_granularity": "week",
            },
        }

    return {
        "series": [
            {
                "key": "gcp_build_count",
                "label": "GCP builds",
                "type": "bar",
                "points": [
                    [bucket_start, weekly_counts["GCP"].get(bucket_start, 0)]
                    for bucket_start in ordered_buckets
                ],
            },
            {
                "key": "idc_build_count",
                "label": "IDC builds",
                "type": "bar",
                "points": [
                    [bucket_start, weekly_counts["IDC"].get(bucket_start, 0)]
                    for bucket_start in ordered_buckets
                ],
            },
        ],
        "meta": {
            **filters.meta(),
            "bucket_granularity": "week",
        },
    }


def get_longest_avg_success_jobs(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
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
                  FROM ci_l1_builds b
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
                  FROM ci_l1_builds b
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
        branch_name = f"COALESCE(NULLIF({branch_expr('b')}, ''), '(unknown branch)')"
        rows = connection.execute(
            text(
                f"""
                SELECT
                  UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                  b.repo_full_name AS repo_name,
                  {branch_name} AS branch_name,
                  COUNT(*) AS build_count
                FROM ci_l1_builds b
                WHERE {where_clause}
                  AND UPPER(COALESCE(b.cloud_phase, '')) IN ('GCP', 'IDC')
                GROUP BY UPPER(COALESCE(b.cloud_phase, '')), b.repo_full_name, {branch_name}
                ORDER BY UPPER(COALESCE(b.cloud_phase, '')), build_count DESC, b.repo_full_name, {branch_name}
                """
            ),
            params,
        ).mappings()

        cloud_repo_counts: dict[str, dict[str, dict[str, Any]]] = {"GCP": {}, "IDC": {}}
        cloud_totals = {"GCP": 0, "IDC": 0}
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
    for cloud_phase in ("GCP", "IDC"):
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
        end_date=None,
        granularity=filters.granularity,
    )

    with engine.begin() as connection:
        where_clause, params = build_common_where(scope_filters, table_alias="b")
        success_where = success_expr("b")
        normalized_job_path = _normalized_job_path_expr(connection, "b")
        anchor_end_date = filters.end_date or _find_latest_gcp_success_date(
            connection,
            where_clause,
            params,
            success_where,
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
                },
            }

        anchor_end_exclusive = datetime.combine(anchor_end_date + timedelta(days=1), time.min)
        recent_window_start = anchor_end_exclusive - timedelta(days=MIGRATION_WINDOW_DAYS)
        baseline_window_start = _datetime_shift_expr(
            connection,
            "fg.first_gcp_success_at",
            -MIGRATION_WINDOW_DAYS,
        )

        rows = connection.execute(
            text(
                f"""
                WITH scoped_success_builds AS (
                  SELECT
                    b.job_name,
                    {normalized_job_path} AS normalized_job_path,
                    UPPER(COALESCE(b.cloud_phase, '')) AS cloud_phase,
                    b.start_time,
                    b.run_seconds
                  FROM ci_l1_builds b
                  WHERE {where_clause}
                    AND {success_where}
                    AND b.run_seconds IS NOT NULL
                    AND b.start_time < :anchor_end_exclusive
                    AND {normalized_job_path} IS NOT NULL
                ),
                first_gcp AS (
                  SELECT
                    s.normalized_job_path,
                    MIN(s.start_time) AS first_gcp_success_at,
                    MIN(s.job_name) AS job_name
                  FROM scoped_success_builds s
                  WHERE s.cloud_phase = 'GCP'
                  GROUP BY s.normalized_job_path
                ),
                recent_gcp AS (
                  SELECT
                    s.normalized_job_path,
                    MIN(s.job_name) AS job_name,
                    COUNT(*) AS gcp_success_count,
                    AVG(s.run_seconds) AS gcp_recent_avg_run_s
                  FROM scoped_success_builds s
                  WHERE s.cloud_phase = 'GCP'
                    AND s.start_time >= :recent_window_start
                  GROUP BY s.normalized_job_path
                ),
                idc_baseline AS (
                  SELECT
                    s.normalized_job_path,
                    COUNT(*) AS idc_success_count,
                    AVG(s.run_seconds) AS idc_baseline_avg_run_s
                  FROM scoped_success_builds s
                  JOIN first_gcp fg
                    ON fg.normalized_job_path = s.normalized_job_path
                  WHERE s.cloud_phase = 'IDC'
                    AND s.start_time >= {baseline_window_start}
                    AND s.start_time < fg.first_gcp_success_at
                  GROUP BY s.normalized_job_path
                )
                SELECT
                  COALESCE(rg.job_name, fg.job_name) AS job_name,
                  fg.normalized_job_path,
                  fg.first_gcp_success_at,
                  ib.idc_success_count,
                  rg.gcp_success_count,
                  ib.idc_baseline_avg_run_s,
                  rg.gcp_recent_avg_run_s,
                  rg.gcp_recent_avg_run_s - ib.idc_baseline_avg_run_s AS delta_run_s,
                  CASE
                    WHEN ib.idc_baseline_avg_run_s = 0 THEN 0
                    ELSE ROUND(
                      ((rg.gcp_recent_avg_run_s - ib.idc_baseline_avg_run_s) * 100.0)
                      / ib.idc_baseline_avg_run_s,
                      2
                    )
                  END AS delta_pct
                FROM first_gcp fg
                JOIN recent_gcp rg
                  ON rg.normalized_job_path = fg.normalized_job_path
                JOIN idc_baseline ib
                  ON ib.normalized_job_path = fg.normalized_job_path
                WHERE ib.idc_success_count >= :min_success_runs_each_side
                  AND rg.gcp_success_count >= :min_success_runs_each_side
                ORDER BY delta_run_s ASC, fg.normalized_job_path ASC
                """
            ),
            {
                **params,
                "anchor_end_exclusive": anchor_end_exclusive,
                "recent_window_start": recent_window_start,
                "min_success_runs_each_side": MIGRATION_MIN_SUCCESS_RUNS,
            },
        ).mappings()

        items = [
            {
                "job_name": str(row["job_name"]),
                "normalized_job_path": str(row["normalized_job_path"]),
                "idc_baseline_avg_run_s": round(float(row["idc_baseline_avg_run_s"] or 0)),
                "gcp_recent_avg_run_s": round(float(row["gcp_recent_avg_run_s"] or 0)),
                "delta_run_s": round(float(row["delta_run_s"] or 0)),
                "delta_pct": round(float(row["delta_pct"] or 0), 2),
                "idc_success_count": int(row["idc_success_count"] or 0),
                "gcp_success_count": int(row["gcp_success_count"] or 0),
                "first_gcp_success_at": _coerce_isoformat_utc(row["first_gcp_success_at"]),
            }
            for row in rows
        ]

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
        },
    }


def _find_latest_gcp_success_date(
    connection,
    where_clause: str,
    params: dict[str, Any],
    success_where: str,
):
    row = connection.execute(
        text(
            f"""
            SELECT MAX(DATE(b.start_time)) AS anchor_end_date
            FROM ci_l1_builds b
            WHERE {where_clause}
              AND {success_where}
              AND UPPER(COALESCE(b.cloud_phase, '')) = 'GCP'
            """
        ),
        params,
    ).mappings().one()
    if row["anchor_end_date"] is None:
        return None
    value = row["anchor_end_date"]
    if hasattr(value, "isoformat"):
        return value
    return datetime.fromisoformat(str(value)).date()


def _normalized_job_path_expr(connection, table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    base_key = f"NULLIF({prefix}normalized_build_key, '')"

    if connection.dialect.name == "sqlite":
        return f"normalized_job_path_from_key({base_key})"

    return (
        "CASE "
        f"WHEN {base_key} IS NULL THEN NULL "
        f"ELSE NULLIF(REGEXP_REPLACE({base_key}, '/[0-9]+$', ''), '') "
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
