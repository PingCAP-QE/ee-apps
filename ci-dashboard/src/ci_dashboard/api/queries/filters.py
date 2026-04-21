from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.api.queries.base import (
    CommonFilters,
    branch_expr,
    builds_table_expr,
    build_common_where,
)


def list_repos(
    engine: Engine,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, object]:
    filters = CommonFilters(start_date=start_date, end_date=end_date)
    where_clause, params = build_common_where(filters, table_alias="b")

    with engine.begin() as connection:
        builds_table = builds_table_expr(connection, filters, alias="b")
        rows = connection.execute(
            text(
                f"""
                SELECT DISTINCT repo_full_name
                FROM {builds_table}
                WHERE {where_clause}
                ORDER BY repo_full_name
                """
            ),
            params,
        ).mappings()

        items = [
            {"value": row["repo_full_name"], "label": row["repo_full_name"]}
            for row in rows
            if row["repo_full_name"]
        ]

    return {"items": items}


def list_branches(
    engine: Engine,
    *,
    repo: str | None = None,
) -> dict[str, object]:
    filters = CommonFilters(repo=repo)
    where_clause, params = build_common_where(filters, table_alias="b")

    with engine.begin() as connection:
        builds_table = builds_table_expr(connection, filters, alias="b")
        effective_branch = branch_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT DISTINCT {effective_branch} AS branch_name
                FROM {builds_table}
                WHERE {where_clause}
                  AND {effective_branch} IS NOT NULL
                  AND {effective_branch} <> ''
                ORDER BY branch_name
                """
            ),
            params,
        ).mappings()

        items = [
            {"value": row["branch_name"], "label": row["branch_name"]}
            for row in rows
            if row["branch_name"]
        ]

    return {"items": items}


def list_jobs(
    engine: Engine,
    *,
    repo: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    filters = CommonFilters(repo=repo, branch=branch)
    where_clause, params = build_common_where(filters, table_alias="b")

    with engine.begin() as connection:
        builds_table = builds_table_expr(connection, filters, alias="b")
        rows = connection.execute(
            text(
                f"""
                SELECT DISTINCT job_name
                FROM {builds_table}
                WHERE {where_clause}
                ORDER BY job_name
                """
            ),
            params,
        ).mappings()

        items = [
            {"value": row["job_name"], "label": row["job_name"]}
            for row in rows
            if row["job_name"]
        ]

    return {"items": items}


def list_cloud_phases(engine: Engine) -> dict[str, object]:
    filters = CommonFilters()
    with engine.begin() as connection:
        builds_table = builds_table_expr(connection, filters, alias="b")
        rows = connection.execute(
            text(
                f"""
                SELECT DISTINCT cloud_phase
                FROM {builds_table}
                WHERE b.cloud_phase IS NOT NULL
                  AND b.cloud_phase <> ''
                ORDER BY cloud_phase
                """
            )
        ).mappings()

        items = [
            {"value": row["cloud_phase"], "label": row["cloud_phase"]}
            for row in rows
            if row["cloud_phase"]
        ]

    return {"items": items}
