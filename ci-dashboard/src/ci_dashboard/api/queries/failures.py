from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.api.queries.base import (
    CommonFilters,
    bucket_expr,
    build_common_where,
    filter_complete_week_rows,
)


def get_failure_category_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  SUM(CASE WHEN LOWER(b.state) IN ('failure', 'error', 'timeout', 'timed_out', 'aborted')
                             AND b.failure_category = 'FLAKY_TEST'
                           THEN 1 ELSE 0 END) AS flaky_test_count,
                  SUM(CASE WHEN LOWER(b.state) IN ('failure', 'error', 'timeout', 'timed_out', 'aborted')
                             AND b.failure_category IS NULL
                           THEN 1 ELSE 0 END) AS unclassified_count
                FROM ci_l1_builds b
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

        flaky_points: list[list[Any]] = []
        unclassified_points: list[list[Any]] = []
        for row in data_rows:
            bucket_start = str(row["bucket_start"])
            flaky_points.append([bucket_start, int(row["flaky_test_count"] or 0)])
            unclassified_points.append([bucket_start, int(row["unclassified_count"] or 0)])

    return {
        "series": [
            {"key": "FLAKY_TEST", "type": "bar", "points": flaky_points},
            {"key": "UNCLASSIFIED", "type": "bar", "points": unclassified_points},
        ],
        "meta": filters.meta(),
    }


def get_failure_category_share(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = build_common_where(filters, table_alias="b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  b.cloud_phase,
                  SUM(CASE WHEN LOWER(b.state) IN ('failure', 'error', 'timeout', 'timed_out', 'aborted')
                             AND b.failure_category = 'FLAKY_TEST'
                           THEN 1 ELSE 0 END) AS flaky_test_count,
                  SUM(CASE WHEN LOWER(b.state) IN ('failure', 'error', 'timeout', 'timed_out', 'aborted')
                             AND b.failure_category IS NULL
                           THEN 1 ELSE 0 END) AS unclassified_count
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
            groups.append(
                {
                    "name": row["cloud_phase"],
                    "values": [
                        int(row["flaky_test_count"] or 0),
                        int(row["unclassified_count"] or 0),
                    ],
                }
            )

    return {
        "categories": ["FLAKY_TEST", "UNCLASSIFIED"],
        "groups": groups,
        "meta": filters.meta(),
    }
