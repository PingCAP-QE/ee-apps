from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.models import RepairJobNamesSummary


def run_repair_job_names(engine: Engine) -> RepairJobNamesSummary:
    with engine.begin() as connection:
        build_short_before = _count_short_build_job_names(connection)
        pod_short_before = _count_short_pod_job_names(connection)
        build_rows_updated = _repair_build_job_names(connection)
        pod_rows_updated = _repair_pod_job_names(connection)
        build_short_after = _count_short_build_job_names(connection)
        pod_short_after = _count_short_pod_job_names(connection)

    return RepairJobNamesSummary(
        build_rows_updated=build_rows_updated,
        pod_rows_updated=pod_rows_updated,
        build_short_before=build_short_before,
        build_short_after=build_short_after,
        pod_short_before=pod_short_before,
        pod_short_after=pod_short_after,
    )


def _count_short_build_job_names(connection: Connection) -> int:
    row = connection.execute(
        text(
            """
            SELECT COUNT(*) AS count
            FROM ci_l1_builds
            WHERE job_name IS NOT NULL
              AND job_name <> ''
              AND job_name NOT LIKE '%/%'
              AND job_name NOT LIKE '% %'
              AND repo_full_name IS NOT NULL
              AND repo_full_name <> ''
            """
        )
    ).mappings().one()
    return int(row["count"] or 0)


def _count_short_pod_job_names(connection: Connection) -> int:
    row = connection.execute(
        text(
            """
            SELECT COUNT(*) AS count
            FROM ci_l1_pod_lifecycle
            WHERE (
                (job_name IS NOT NULL
                 AND job_name <> ''
                 AND job_name NOT LIKE '%/%'
                 AND job_name NOT LIKE '% %'
                 AND repo_full_name IS NOT NULL
                 AND repo_full_name <> '')
              OR (
                 (job_name IS NULL OR job_name = '')
                 AND ci_job IS NOT NULL
                 AND ci_job <> ''
                 AND ci_job LIKE '%/%'
              )
            )
            """
        )
    ).mappings().one()
    return int(row["count"] or 0)


def _repair_build_job_names(connection: Connection) -> int:
    expr = _concat_full_job_name_expr(connection, "repo_full_name", "job_name")
    result = connection.execute(
        text(
            f"""
            UPDATE ci_l1_builds
            SET job_name = {expr}
            WHERE job_name IS NOT NULL
              AND job_name <> ''
              AND job_name NOT LIKE '%/%'
              AND job_name NOT LIKE '% %'
              AND repo_full_name IS NOT NULL
              AND repo_full_name <> ''
            """
        )
    )
    return int(result.rowcount or 0)


def _repair_pod_job_names(connection: Connection) -> int:
    prefixed_expr = _concat_full_job_name_expr(connection, "repo_full_name", "job_name")
    result = connection.execute(
        text(
            f"""
            UPDATE ci_l1_pod_lifecycle
            SET job_name = CASE
              WHEN ci_job IS NOT NULL
               AND ci_job <> ''
               AND ci_job LIKE '%/%'
              THEN ci_job
              WHEN job_name IS NOT NULL
               AND job_name <> ''
               AND job_name NOT LIKE '%/%'
               AND job_name NOT LIKE '% %'
               AND repo_full_name IS NOT NULL
               AND repo_full_name <> ''
              THEN {prefixed_expr}
              ELSE job_name
            END
            WHERE (
                (job_name IS NOT NULL
                 AND job_name <> ''
                 AND job_name NOT LIKE '%/%'
                 AND job_name NOT LIKE '% %'
                 AND repo_full_name IS NOT NULL
                 AND repo_full_name <> '')
              OR (
                 (job_name IS NULL OR job_name = '')
                 AND ci_job IS NOT NULL
                 AND ci_job <> ''
                 AND ci_job LIKE '%/%'
              )
            )
            """
        )
    )
    return int(result.rowcount or 0)


def _concat_full_job_name_expr(connection: Connection, repo_expr: str, job_expr: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"({repo_expr} || '/' || {job_expr})"
    return f"CONCAT({repo_expr}, '/', {job_expr})"
