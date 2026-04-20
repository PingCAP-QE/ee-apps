from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.api.queries.base import (
    CommonFilters,
    MAX_RANKING_LIMIT,
    branch_match_expr,
    bucket_expr,
    filter_complete_week_rows,
    failure_like_expr,
    rate_pct,
    to_number,
)

DEFAULT_DISTINCT_CASE_REPO = "pingcap/tidb"
UNKNOWN_ISSUE_BRANCH = "__unknown__"


def get_flaky_trend(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    rows = _query_bucketed_flaky_metrics(engine, filters)

    flaky_points: list[list[Any]] = []
    total_points: list[list[Any]] = []
    for row in rows:
        bucket = str(row["bucket_start"])
        total_failure_like = int(row["total_failure_like_count"] or 0)
        flaky_build_count = int(row["flaky_build_count"] or 0)
        flaky_points.append([bucket, rate_pct(flaky_build_count, total_failure_like)])
        total_points.append([bucket, total_failure_like])

    return {
        "series": [
            {"key": "flaky_rate_pct", "type": "line", "axis": "right", "points": flaky_points},
            {"key": "total_failure_like_count", "type": "bar", "axis": "left", "points": total_points},
        ],
        "meta": filters.meta(),
    }


def get_flaky_composition(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    rows = _query_bucketed_flaky_metrics(engine, filters)

    flaky_points: list[list[Any]] = []
    retry_points: list[list[Any]] = []
    noisy_points: list[list[Any]] = []
    total_points: list[list[Any]] = []
    for row in rows:
        bucket = str(row["bucket_start"])
        total_failure_like = int(row["total_failure_like_count"] or 0)
        flaky_build_count = int(row["flaky_build_count"] or 0)
        retry_loop_build_count = int(row["retry_loop_build_count"] or 0)
        noisy_build_count = int(row["noisy_build_count"] or 0)

        flaky_points.append([bucket, rate_pct(flaky_build_count, total_failure_like)])
        retry_points.append([bucket, rate_pct(retry_loop_build_count, total_failure_like)])
        noisy_points.append([bucket, rate_pct(noisy_build_count, total_failure_like)])
        total_points.append([bucket, total_failure_like])

    return {
        "series": [
            {"key": "flaky_rate_pct", "type": "line", "axis": "right", "points": flaky_points},
            {"key": "retry_loop_rate_pct", "type": "line", "axis": "right", "points": retry_points},
            {"key": "noisy_rate_pct", "type": "line", "axis": "right", "points": noisy_points},
            {"key": "total_failure_like_count", "type": "bar", "axis": "left", "points": total_points},
        ],
        "meta": filters.meta(),
    }


def get_flaky_top_jobs(
    engine: Engine,
    filters: CommonFilters,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    effective_limit = min(limit, MAX_RANKING_LIMIT)
    where_clause, params = _build_build_where(filters, table_alias="b")
    failure_like = failure_like_expr("b")

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                f"""
                WITH job_stats AS (
                  SELECT
                    b.job_name,
                    SUM(CASE WHEN {failure_like} THEN 1 ELSE 0 END) AS failure_like_build_count,
                    SUM(CASE WHEN {failure_like} AND b.is_flaky = 1 THEN 1 ELSE 0 END) AS flaky_build_count,
                    SUM(CASE WHEN {failure_like} AND b.is_retry_loop = 1 THEN 1 ELSE 0 END) AS retry_loop_build_count,
                    SUM(CASE WHEN {failure_like} AND (b.is_flaky = 1 OR b.is_retry_loop = 1) THEN 1 ELSE 0 END) AS noisy_build_count
                  FROM ci_l1_builds b
                  WHERE {where_clause}
                  GROUP BY b.job_name
                )
                SELECT
                  job_name,
                  failure_like_build_count,
                  flaky_build_count,
                  retry_loop_build_count,
                  noisy_build_count,
                  CASE
                    WHEN failure_like_build_count > 0
                    THEN noisy_build_count * 100.0 / failure_like_build_count
                    ELSE 0
                  END AS noisy_rate_pct
                FROM job_stats
                WHERE failure_like_build_count > 0
                ORDER BY noisy_rate_pct DESC, noisy_build_count DESC, failure_like_build_count DESC, job_name ASC
                LIMIT :limit
                """
            ),
            {**params, "limit": effective_limit},
        ).mappings()

        items = []
        for row in rows:
            failure_like_build_count = int(row["failure_like_build_count"] or 0)
            noisy_build_count = int(row["noisy_build_count"] or 0)
            items.append(
                {
                    "name": row["job_name"],
                    "value": to_number(row["noisy_rate_pct"]),
                    "failure_like_build_count": failure_like_build_count,
                    "flaky_build_count": int(row["flaky_build_count"] or 0),
                    "retry_loop_build_count": int(row["retry_loop_build_count"] or 0),
                    "noisy_build_count": noisy_build_count,
                    "noisy_rate_pct": to_number(row["noisy_rate_pct"]),
                }
            )

    meta = filters.meta()
    meta["limit"] = effective_limit
    meta["value_key"] = "noisy_rate_pct"
    return {"items": items, "meta": meta}


def get_flaky_period_comparison(
    engine: Engine,
    *,
    repo: str | None,
    branch: str | None,
    job_name: str | None,
    cloud_phase: str | None,
    period_a_start: date,
    period_a_end: date,
    period_b_start: date,
    period_b_end: date,
) -> dict[str, Any]:
    period_a_filters = CommonFilters(
        repo=repo,
        branch=branch,
        job_name=job_name,
        cloud_phase=cloud_phase,
        start_date=period_a_start,
        end_date=period_a_end,
    )
    period_b_filters = CommonFilters(
        repo=repo,
        branch=branch,
        job_name=job_name,
        cloud_phase=cloud_phase,
        start_date=period_b_start,
        end_date=period_b_end,
    )

    period_a_summary = _query_period_summary(engine, period_a_filters)
    period_b_summary = _query_period_summary(engine, period_b_filters)

    return {
        "groups": [
            {"name": "period_a", "values": period_a_summary},
            {"name": "period_b", "values": period_b_summary},
        ],
        "meta": {
            "repo": repo,
            "branch": branch,
            "job_name": job_name,
            "cloud_phase": cloud_phase,
            "period_a_start": period_a_start.isoformat(),
            "period_a_end": period_a_end.isoformat(),
            "period_b_start": period_b_start.isoformat(),
            "period_b_end": period_b_end.isoformat(),
        },
    }


def get_distinct_flaky_case_counts_by_branch(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any]:
    effective_repo = filters.repo or DEFAULT_DISTINCT_CASE_REPO

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                f"""
                WITH target_prs AS (
                  SELECT DISTINCT repo, pr_number, target_branch
                  FROM ci_l1_pr_events
                  WHERE repo = :repo
                    AND pr_number IS NOT NULL
                    AND target_branch IS NOT NULL
                    AND target_branch <> ''
                    {_optional_clause(filters.branch, "AND target_branch = :branch")}
                ),
                build_scope AS (
                  SELECT DISTINCT
                    p.target_branch AS branch,
                    {bucket_expr(connection, "b.start_time", "week")} AS week_start,
                    b.start_time,
                    b.normalized_build_key,
                    UPPER(COALESCE(NULLIF(b.cloud_phase, ''), 'IDC')) AS cloud_phase
                  FROM ci_l1_builds b
                  JOIN target_prs p
                    ON p.repo = b.repo_full_name
                   AND p.pr_number = b.pr_number
                  WHERE b.repo_full_name = :repo
                    AND b.pr_number IS NOT NULL
                    AND b.normalized_build_key IS NOT NULL
                    {_optional_clause(filters.job_name, "AND b.job_name = :job_name")}
                    {_optional_clause(filters.cloud_phase, "AND UPPER(COALESCE(NULLIF(b.cloud_phase, ''), 'IDC')) = :cloud_phase")}
                    {_optional_clause(filters.start_date, "AND b.start_time >= :start_time_from")}
                    {_optional_clause(filters.end_date, "AND b.start_time < :start_time_to")}
                ),
                case_runs_raw AS (
                  SELECT
                    pcr.branch,
                    pcr.case_name,
                    {_normalize_case_build_key_expr(connection, "pcr.build_url")} AS build_key,
                    {_case_cloud_phase_expr("pcr.build_url")} AS cloud_phase,
                    pcr.report_time,
                    CASE WHEN pcr.flaky = 1 THEN 1 ELSE 0 END AS flaky_flag
                  FROM problem_case_runs pcr
                  WHERE pcr.repo = :repo
                    AND pcr.branch IS NOT NULL
                    AND pcr.branch <> ''
                    AND pcr.case_name IS NOT NULL
                    AND pcr.case_name <> ''
                    {_optional_clause(filters.branch, "AND pcr.branch = :branch")}
                    {_optional_clause(filters.start_date, "AND pcr.report_time >= :case_report_time_from")}
                    {_optional_clause(filters.end_date, "AND pcr.report_time < :case_report_time_to")}
                ),
                case_runs AS (
                  SELECT
                    branch,
                    case_name,
                    build_key,
                    cloud_phase,
                    report_time,
                    MAX(flaky_flag) AS flaky_flag
                  FROM case_runs_raw
                  GROUP BY branch, case_name, build_key, cloud_phase, report_time
                )
                SELECT
                  bs.branch,
                  bs.week_start,
                  COUNT(DISTINCT cr.case_name) AS distinct_flaky_case_count
                FROM build_scope bs
                JOIN case_runs cr
                  ON cr.build_key = bs.normalized_build_key
                 AND cr.branch = bs.branch
                 AND cr.cloud_phase = bs.cloud_phase
                 AND {_case_build_time_match_expr(connection, "cr.report_time", "bs.start_time")}
                WHERE cr.flaky_flag = 1
                GROUP BY bs.branch, bs.week_start
                ORDER BY bs.branch, bs.week_start
                """
            ),
            _distinct_case_params(filters, effective_repo),
        ).mappings()
        data_rows = [dict(row) for row in rows]

    week_columns = _week_columns(filters.start_date, filters.end_date, data_rows)
    counts_by_branch_week: dict[str, dict[str, int]] = {}
    for row in data_rows:
        branch = str(row["branch"])
        week_start = str(row["week_start"])
        counts_by_branch_week.setdefault(branch, {})[week_start] = int(
            row["distinct_flaky_case_count"] or 0
        )

    branches = _ordered_branches(counts_by_branch_week.keys(), filters.branch)
    rows_payload = [
        {
            "branch": branch,
            "values": [counts_by_branch_week.get(branch, {}).get(week, 0) for week in week_columns],
        }
        for branch in branches
    ]

    meta = filters.meta()
    meta.update(
        {
            "requested_repo": filters.repo,
            "effective_repo": effective_repo,
            "bucket_granularity": "week",
            "defaulted_repo": filters.repo is None,
        }
    )
    return {
        "weeks": week_columns,
        "rows": rows_payload,
        "meta": meta,
    }


def get_flaky_case_flow_v2(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any]:
    """V2: confirm state changes using two consecutive weeks to reduce jitter."""
    effective_repo = filters.repo or DEFAULT_DISTINCT_CASE_REPO
    with engine.begin() as connection:
        presence_rows = _fetch_weekly_flaky_case_presence(connection, filters, effective_repo)

    week_columns = _week_columns(filters.start_date, filters.end_date, presence_rows)
    presence_by_case: dict[tuple[str, str], set[str]] = {}
    for row in presence_rows:
        key = (str(row["branch"]), str(row["case_name"]))
        presence_by_case.setdefault(key, set()).add(str(row["week_start"]))

    new_by_week = {week: 0 for week in week_columns}
    resolved_by_week = {week: 0 for week in week_columns}
    new_by_branch_week: dict[str, dict[str, int]] = {}
    resolved_by_branch_week: dict[str, dict[str, int]] = {}

    for (branch, _case_name), presence_weeks in presence_by_case.items():
        presence_bits = [week in presence_weeks for week in week_columns]
        for index, week in enumerate(week_columns):
            # Confirm "new" on the 2nd consecutive flaky week.
            # A missing week before range start is treated as non-flaky.
            is_new = (
                index >= 1
                and presence_bits[index]
                and presence_bits[index - 1]
                and (index < 2 or not presence_bits[index - 2])
            )
            if is_new:
                new_by_week[week] += 1
                new_by_branch_week.setdefault(branch, {}).setdefault(week, 0)
                new_by_branch_week[branch][week] += 1

            # Confirm "resolved" on the 2nd consecutive non-flaky week
            # after the case was flaky in the preceding week.
            is_resolved = (
                index >= 2
                and not presence_bits[index]
                and not presence_bits[index - 1]
                and presence_bits[index - 2]
            )
            if is_resolved:
                resolved_by_week[week] += 1
                resolved_by_branch_week.setdefault(branch, {}).setdefault(week, 0)
                resolved_by_branch_week[branch][week] += 1

    net_by_week = {
        week: new_by_week.get(week, 0) - resolved_by_week.get(week, 0)
        for week in week_columns
    }

    summary_rows = [
        {
            "week_start": week,
            "new_case_count": new_by_week.get(week, 0),
            "resolved_case_count": resolved_by_week.get(week, 0),
            "net_case_count": net_by_week.get(week, 0),
        }
        for week in week_columns
    ]

    branch_rows = [
        {
            "branch": branch,
            "new_values": [new_by_branch_week.get(branch, {}).get(week, 0) for week in week_columns],
            "resolved_values": [
                resolved_by_branch_week.get(branch, {}).get(week, 0) for week in week_columns
            ],
        }
        for branch in _ordered_branches(
            {branch for branch, _case_name in presence_by_case.keys()},
            filters.branch,
        )
    ]

    meta = filters.meta()
    meta.update(
        {
            "requested_repo": filters.repo,
            "effective_repo": effective_repo,
            "bucket_granularity": "week",
            "defaulted_repo": filters.repo is None,
            "confirmation_rule": "2_consecutive_weeks",
            "week_count": len(week_columns),
            "case_count_in_scope": len(presence_by_case),
        }
    )
    return {
        "weeks": week_columns,
        "series": [
            {
                "key": "new_case_count",
                "label": "New (2-week confirmed)",
                "type": "bar",
                "axis": "left",
                "points": [[week, new_by_week.get(week, 0)] for week in week_columns],
            },
            {
                "key": "resolved_case_count",
                "label": "Resolved (2-week confirmed)",
                "type": "bar",
                "axis": "left",
                "points": [[week, resolved_by_week.get(week, 0)] for week in week_columns],
            },
        ],
        "summary": summary_rows,
        "rows": branch_rows,
        "meta": meta,
    }


def get_issue_filtered_weekly_case_rates(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any]:
    effective_repo = filters.repo or DEFAULT_DISTINCT_CASE_REPO

    with engine.begin() as connection:
        issue_cases = _fetch_issue_cases(connection, filters, effective_repo)
        data_rows = _fetch_issue_weekly_rate_rows(connection, filters, effective_repo)

    week_columns = _week_columns(filters.start_date, filters.end_date, data_rows)
    metrics_by_case_week: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in data_rows:
        case_key = _issue_case_key(
            issue_branch=row["issue_branch"],
            case_name=row["case_name"],
        )
        week_start = str(row["week_start"])
        metrics_by_case_week.setdefault(case_key, {})[week_start] = _build_rate_metric(
            week_start=week_start,
            flaky_runs=int(row["flaky_runs"] or 0),
            total_runs_est=int(row["total_runs_est"] or 0),
        )

    totals_by_week = {
        week: {"flaky_runs": 0, "total_runs_est": 0}
        for week in week_columns
    }
    rows_payload: list[dict[str, Any]] = []
    for issue in issue_cases:
        case_name = str(issue["case_name"])
        issue_branch = issue["issue_branch"]
        case_key = _issue_case_key(issue_branch=issue_branch, case_name=case_name)
        weekly_metrics = [
            metrics_by_case_week.get(case_key, {}).get(
                week,
                _build_rate_metric(week_start=week, flaky_runs=0, total_runs_est=0),
            )
            for week in week_columns
        ]
        for metric in weekly_metrics:
            totals_by_week[metric["week_start"]]["flaky_runs"] += metric["flaky_runs"]
            totals_by_week[metric["week_start"]]["total_runs_est"] += metric["total_runs_est"]

        rows_payload.append(
            {
                "case_name": case_name,
                "display_name": _issue_display_name(
                    case_name=case_name,
                    issue_branch=issue_branch,
                    selected_branch=filters.branch,
                ),
                "issue_branch": issue_branch,
                "issue_number": int(issue["issue_number"]),
                "issue_url": issue["issue_url"],
                "issue_title": issue["issue_title"],
                "issue_status": issue["issue_status"],
                "issue_created_at": issue["issue_created_at"],
                "issue_closed_at": issue["issue_closed_at"],
                "last_reopened_at": issue["last_reopened_at"],
                "reopen_count": int(issue["reopen_count"] or 0),
                "cells": [metric["cell"] for metric in weekly_metrics],
                "metrics": weekly_metrics,
            }
        )

    trend_points: list[list[Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for week in week_columns:
        metric = _build_rate_metric(
            week_start=week,
            flaky_runs=totals_by_week[week]["flaky_runs"],
            total_runs_est=totals_by_week[week]["total_runs_est"],
        )
        trend_points.append([week, metric["flaky_rate_pct"]])
        summary_rows.append(metric)

    meta = filters.meta()
    meta.update(
        {
            "requested_repo": filters.repo,
            "effective_repo": effective_repo,
            "bucket_granularity": "week",
            "defaulted_repo": filters.repo is None,
            "issue_case_count": len(rows_payload),
        }
    )
    return {
        "weeks": week_columns,
        "rows": rows_payload,
        "trend": {
            "series": [
                {
                    "key": "issue_filtered_flaky_rate_pct",
                    "type": "line",
                    "points": trend_points,
                }
            ],
            "summary": summary_rows,
            "meta": meta,
        },
        "meta": meta,
    }


def get_issue_lifecycle_snapshot(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any]:
    effective_repo = filters.repo or DEFAULT_DISTINCT_CASE_REPO
    latest_full_week_start = _latest_complete_week_start(filters.end_date)
    latest_full_week_end = (
        latest_full_week_start + timedelta(days=6)
        if latest_full_week_start is not None
        else None
    )

    with engine.begin() as connection:
        issue_rows = _fetch_issue_latest_rows(connection, filters, effective_repo)

    scoped_issue_rows = [
        row
        for row in issue_rows
        if _issue_overlaps_window(
            row,
            start_date=filters.start_date,
            end_date=filters.end_date,
        )
    ]

    week_start_dt = (
        datetime.combine(latest_full_week_start, datetime.min.time())
        if latest_full_week_start is not None
        else None
    )
    week_end_exclusive_dt = (
        week_start_dt + timedelta(days=7) if week_start_dt is not None else None
    )

    created_this_week = [
        row
        for row in scoped_issue_rows
        if week_start_dt is not None
        and _parse_datetime_value(row.get("issue_created_at")) is not None
        and week_start_dt
        <= _parse_datetime_value(row.get("issue_created_at"))
        < week_end_exclusive_dt
    ]
    closed_this_week = [
        row
        for row in scoped_issue_rows
        if week_start_dt is not None
        and _parse_datetime_value(row.get("issue_closed_at")) is not None
        and week_start_dt
        <= _parse_datetime_value(row.get("issue_closed_at"))
        < week_end_exclusive_dt
    ]
    reopened_this_week = [
        row
        for row in scoped_issue_rows
        if int(row.get("reopen_count") or 0) > 0
        and week_start_dt is not None
        and _parse_datetime_value(row.get("last_reopened_at")) is not None
        and week_start_dt
        <= _parse_datetime_value(row.get("last_reopened_at"))
        < week_end_exclusive_dt
    ]

    meta = filters.meta()
    meta.update(
        {
            "requested_repo": filters.repo,
            "effective_repo": effective_repo,
            "defaulted_repo": filters.repo is None,
            "latest_full_week_start": (
                latest_full_week_start.isoformat() if latest_full_week_start else None
            ),
            "latest_full_week_end": (
                latest_full_week_end.isoformat() if latest_full_week_end else None
            ),
            "ignores_issue_status": True,
        }
    )

    return {
        "meta": meta,
        "scoped_issue_count": len(scoped_issue_rows),
        "scoped_open_count": sum(
            1
            for row in scoped_issue_rows
            if str(row.get("issue_status") or "").lower() == "open"
        ),
        "scoped_closed_count": sum(
            1
            for row in scoped_issue_rows
            if str(row.get("issue_status") or "").lower() == "closed"
        ),
        "latest_week_created_count": len(created_this_week),
        "latest_week_created_open_count": sum(
            1
            for row in created_this_week
            if str(row.get("issue_status") or "").lower() == "open"
        ),
        "latest_week_created_closed_count": sum(
            1
            for row in created_this_week
            if str(row.get("issue_status") or "").lower() == "closed"
        ),
        "latest_week_closed_count": len(closed_this_week),
        "latest_week_reopened_count": len(reopened_this_week),
    }


def get_issue_lifecycle_weekly(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any]:
    effective_repo = filters.repo or DEFAULT_DISTINCT_CASE_REPO
    with engine.begin() as connection:
        issue_rows = _fetch_issue_latest_rows(connection, filters, effective_repo)

    scoped_issue_rows = [
        row
        for row in issue_rows
        if _issue_overlaps_window(
            row,
            start_date=filters.start_date,
            end_date=filters.end_date,
        )
    ]

    weeks = _week_columns(
        filters.start_date,
        filters.end_date,
        [
            {"week_start": _week_start_iso_from_datetime(_parse_datetime_value(row.get("issue_created_at")))}
            for row in scoped_issue_rows
            if _parse_datetime_value(row.get("issue_created_at")) is not None
        ],
    )
    created_by_week = {week: 0 for week in weeks}
    closed_by_week = {week: 0 for week in weeks}
    reopened_by_week = {week: 0 for week in weeks}

    for row in scoped_issue_rows:
        created_at = _parse_datetime_value(row.get("issue_created_at"))
        if created_at is not None:
            week = _week_start_iso_from_datetime(created_at)
            if week in created_by_week:
                created_by_week[week] += 1

        closed_at = _parse_datetime_value(row.get("issue_closed_at"))
        if closed_at is not None:
            week = _week_start_iso_from_datetime(closed_at)
            if week in closed_by_week:
                closed_by_week[week] += 1

        reopened_at = _parse_datetime_value(row.get("last_reopened_at"))
        if int(row.get("reopen_count") or 0) > 0 and reopened_at is not None:
            week = _week_start_iso_from_datetime(reopened_at)
            if week in reopened_by_week:
                reopened_by_week[week] += 1

    meta = filters.meta()
    meta.update(
        {
            "requested_repo": filters.repo,
            "effective_repo": effective_repo,
            "defaulted_repo": filters.repo is None,
            "bucket_granularity": "week",
            "ignores_issue_status": True,
        }
    )
    return {
        "weeks": weeks,
        "series": [
            {
                "key": "issue_created_count",
                "label": "Created issues",
                "type": "bar",
                "axis": "left",
                "points": [[week, created_by_week.get(week, 0)] for week in weeks],
            },
            {
                "key": "issue_closed_count",
                "label": "Closed issues",
                "type": "bar",
                "axis": "left",
                "points": [[week, closed_by_week.get(week, 0)] for week in weeks],
            },
            {
                "key": "issue_reopened_count",
                "label": "Reopened issues",
                "type": "bar",
                "axis": "left",
                "points": [[week, reopened_by_week.get(week, 0)] for week in weeks],
            },
        ],
        "meta": meta,
    }


def _query_bucketed_flaky_metrics(
    engine: Engine,
    filters: CommonFilters,
) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        where_clause, params = _build_build_where(filters, table_alias="b")
        bucket = bucket_expr(connection, "b.start_time", filters.granularity)
        failure_like = failure_like_expr("b")
        rows = connection.execute(
            text(
                f"""
                SELECT
                  {bucket} AS bucket_start,
                  SUM(CASE WHEN {failure_like} THEN 1 ELSE 0 END) AS total_failure_like_count,
                  SUM(CASE WHEN {failure_like} AND b.is_flaky = 1 THEN 1 ELSE 0 END) AS flaky_build_count,
                  SUM(CASE WHEN {failure_like} AND b.is_retry_loop = 1 THEN 1 ELSE 0 END) AS retry_loop_build_count,
                  SUM(CASE WHEN {failure_like} AND (b.is_flaky = 1 OR b.is_retry_loop = 1) THEN 1 ELSE 0 END) AS noisy_build_count
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
        return data_rows


def _query_period_summary(
    engine: Engine,
    filters: CommonFilters,
) -> dict[str, Any]:
    with engine.begin() as connection:
        where_clause, params = _build_build_where(filters, table_alias="b")
        summary = _fetch_period_summary(connection, where_clause, params)

    total_build_count = int(summary["total_build_count"] or 0)
    failure_like_build_count = int(summary["failure_like_build_count"] or 0)
    flaky_build_count = int(summary["flaky_build_count"] or 0)
    retry_loop_build_count = int(summary["retry_loop_build_count"] or 0)
    noisy_build_count = int(summary["noisy_build_count"] or 0)
    total_pr_count = int(summary["total_pr_count"] or 0)
    affected_pr_count = int(summary["affected_pr_count"] or 0)

    return {
        "total_build_count": total_build_count,
        "failure_like_build_count": failure_like_build_count,
        "flaky_build_count": flaky_build_count,
        "retry_loop_build_count": retry_loop_build_count,
        "noisy_build_count": noisy_build_count,
        "total_pr_count": total_pr_count,
        "affected_pr_count": affected_pr_count,
        "flaky_rate_pct": rate_pct(flaky_build_count, failure_like_build_count),
        "retry_loop_rate_pct": rate_pct(retry_loop_build_count, failure_like_build_count),
        "noisy_rate_pct": rate_pct(noisy_build_count, failure_like_build_count),
        "affected_pr_rate_pct": rate_pct(affected_pr_count, total_pr_count),
    }


def _fetch_period_summary(
    connection: Connection,
    where_clause: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    failure_like = failure_like_expr("b")
    distinct_pr_key = _distinct_pr_key_expr(connection, "b")
    row = connection.execute(
        text(
            f"""
            SELECT
              COUNT(*) AS total_build_count,
              SUM(CASE WHEN {failure_like} THEN 1 ELSE 0 END) AS failure_like_build_count,
              SUM(CASE WHEN {failure_like} AND b.is_flaky = 1 THEN 1 ELSE 0 END) AS flaky_build_count,
              SUM(CASE WHEN {failure_like} AND b.is_retry_loop = 1 THEN 1 ELSE 0 END) AS retry_loop_build_count,
              SUM(CASE WHEN {failure_like} AND (b.is_flaky = 1 OR b.is_retry_loop = 1) THEN 1 ELSE 0 END) AS noisy_build_count,
              COUNT(DISTINCT CASE WHEN b.pr_number IS NOT NULL THEN {distinct_pr_key} END) AS total_pr_count,
              COUNT(DISTINCT CASE
                WHEN b.pr_number IS NOT NULL AND (b.is_flaky = 1 OR b.is_retry_loop = 1) THEN {distinct_pr_key}
              END) AS affected_pr_count
            FROM ci_l1_builds b
            WHERE {where_clause}
            """
        ),
        params,
    ).mappings().one()

    return {key: to_number(value) for key, value in dict(row).items()}


def _fetch_issue_cases(
    connection: Connection,
    filters: CommonFilters,
    effective_repo: str,
) -> list[dict[str, Any]]:
    issue_scope_ctes, params = _build_issue_scope_ctes(filters, effective_repo)
    order_clause = "case_name ASC" if filters.branch else "issue_branch ASC, case_name ASC"
    rows = connection.execute(
        text(
            f"""
            WITH {issue_scope_ctes}
            SELECT
              case_name,
              issue_branch,
              issue_number,
              issue_url,
              issue_title,
              issue_status,
              issue_created_at,
              issue_closed_at,
              last_reopened_at,
              reopen_count
            FROM issue_cases
            ORDER BY {order_clause}
            """
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def _fetch_issue_weekly_rate_rows(
    connection: Connection,
    filters: CommonFilters,
    effective_repo: str,
) -> list[dict[str, Any]]:
    issue_scope_ctes, issue_params = _build_issue_scope_ctes(filters, effective_repo)
    rows = connection.execute(
        text(
            f"""
            WITH {issue_scope_ctes},
            target_prs AS (
              SELECT DISTINCT repo, pr_number, target_branch
              FROM ci_l1_pr_events
              WHERE repo = :repo
                AND pr_number IS NOT NULL
                AND target_branch IS NOT NULL
                AND target_branch <> ''
                {_optional_clause(filters.branch, "AND target_branch = :branch")}
            ),
            build_scope AS (
              SELECT DISTINCT
                p.target_branch AS branch,
                {bucket_expr(connection, "b.start_time", "week")} AS week_start,
                b.start_time,
                b.normalized_build_key,
                b.job_name,
                UPPER(COALESCE(NULLIF(b.cloud_phase, ''), 'IDC')) AS cloud_phase
              FROM ci_l1_builds b
              JOIN target_prs p
                ON p.repo = b.repo_full_name
               AND p.pr_number = b.pr_number
              WHERE b.repo_full_name = :repo
                AND b.pr_number IS NOT NULL
                AND b.normalized_build_key IS NOT NULL
                {_optional_clause(filters.job_name, "AND b.job_name = :job_name")}
                {_optional_clause(filters.cloud_phase, "AND UPPER(COALESCE(NULLIF(b.cloud_phase, ''), 'IDC')) = :cloud_phase")}
                {_optional_clause(filters.start_date, "AND b.start_time >= :start_time_from")}
                {_optional_clause(filters.end_date, "AND b.start_time < :start_time_to")}
            ),
            case_runs_raw AS (
              SELECT
                pcr.branch,
                pcr.case_name,
                {_normalize_case_build_key_expr(connection, "pcr.build_url")} AS build_key,
                {_case_cloud_phase_expr("pcr.build_url")} AS cloud_phase,
                pcr.report_time,
                CASE WHEN pcr.flaky = 1 THEN 1 ELSE 0 END AS flaky_flag
              FROM problem_case_runs pcr
              WHERE pcr.repo = :repo
                AND pcr.branch IS NOT NULL
                AND pcr.branch <> ''
                AND pcr.case_name IS NOT NULL
                AND pcr.case_name <> ''
                {_optional_clause(filters.branch, "AND pcr.branch = :branch")}
                {_optional_clause(filters.start_date, "AND pcr.report_time >= :case_report_time_from")}
                {_optional_clause(filters.end_date, "AND pcr.report_time < :case_report_time_to")}
                AND EXISTS (
                  SELECT 1
                  FROM issue_cases ic
                  WHERE ic.case_name = pcr.case_name
                    AND ic.issue_branch = pcr.branch
                )
            ),
            case_runs AS (
              SELECT
                branch,
                case_name,
                build_key,
                cloud_phase,
                report_time,
                MAX(flaky_flag) AS flaky_flag
              FROM case_runs_raw
              GROUP BY branch, case_name, build_key, cloud_phase, report_time
            ),
            case_job_scope AS (
              SELECT
                ic.case_name,
                ic.issue_branch,
                bs.job_name,
                bs.cloud_phase
              FROM issue_cases ic
              JOIN case_runs cr
                ON cr.case_name = ic.case_name
               AND cr.branch = ic.issue_branch
              JOIN build_scope bs
                ON bs.normalized_build_key = cr.build_key
               AND bs.branch = ic.issue_branch
               AND bs.cloud_phase = cr.cloud_phase
               AND {_case_build_time_match_expr(connection, "cr.report_time", "bs.start_time")}
              GROUP BY ic.case_name, ic.issue_branch, bs.job_name, bs.cloud_phase
            ),
            case_weekly_flaky AS (
              SELECT
                ic.case_name,
                ic.issue_branch,
                bs.week_start,
                SUM(CASE WHEN cr.flaky_flag = 1 THEN 1 ELSE 0 END) AS flaky_runs
              FROM issue_cases ic
              JOIN case_runs cr
                ON cr.case_name = ic.case_name
               AND cr.branch = ic.issue_branch
              JOIN build_scope bs
                ON bs.normalized_build_key = cr.build_key
               AND bs.branch = ic.issue_branch
               AND bs.cloud_phase = cr.cloud_phase
               AND {_case_build_time_match_expr(connection, "cr.report_time", "bs.start_time")}
              GROUP BY ic.case_name, ic.issue_branch, bs.week_start
            ),
            case_weekly_denominator AS (
              SELECT
                cjs.case_name,
                cjs.issue_branch,
                bs.week_start,
                COUNT(DISTINCT bs.normalized_build_key) AS total_runs_est
              FROM case_job_scope cjs
              JOIN build_scope bs
                ON bs.branch = cjs.issue_branch
               AND bs.job_name = cjs.job_name
               AND bs.cloud_phase = cjs.cloud_phase
              GROUP BY cjs.case_name, cjs.issue_branch, bs.week_start
            ),
            case_weekly_keys AS (
              SELECT case_name, issue_branch, week_start
              FROM case_weekly_flaky
              UNION
              SELECT case_name, issue_branch, week_start
              FROM case_weekly_denominator
            )
            SELECT
              cwk.case_name,
              cwk.issue_branch,
              cwk.week_start,
              COALESCE(cwf.flaky_runs, 0) AS flaky_runs,
              COALESCE(cwd.total_runs_est, 0) AS total_runs_est
            FROM case_weekly_keys cwk
            LEFT JOIN case_weekly_flaky cwf
              ON cwf.case_name = cwk.case_name
             AND cwf.issue_branch = cwk.issue_branch
             AND cwf.week_start = cwk.week_start
            LEFT JOIN case_weekly_denominator cwd
              ON cwd.case_name = cwk.case_name
             AND cwd.issue_branch = cwk.issue_branch
             AND cwd.week_start = cwk.week_start
            ORDER BY cwk.issue_branch, cwk.case_name, cwk.week_start
            """
        ),
        {**_distinct_case_params(filters, effective_repo), **issue_params},
    ).mappings()
    return [dict(row) for row in rows]


def _fetch_weekly_flaky_case_presence(
    connection: Connection,
    filters: CommonFilters,
    effective_repo: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            f"""
            WITH target_prs AS (
              SELECT DISTINCT repo, pr_number, target_branch
              FROM ci_l1_pr_events
              WHERE repo = :repo
                AND pr_number IS NOT NULL
                AND target_branch IS NOT NULL
                AND target_branch <> ''
                {_optional_clause(filters.branch, "AND target_branch = :branch")}
            ),
            build_scope AS (
              SELECT DISTINCT
                p.target_branch AS branch,
                {bucket_expr(connection, "b.start_time", "week")} AS week_start,
                b.start_time,
                b.normalized_build_key,
                UPPER(COALESCE(NULLIF(b.cloud_phase, ''), 'IDC')) AS cloud_phase
              FROM ci_l1_builds b
              JOIN target_prs p
                ON p.repo = b.repo_full_name
               AND p.pr_number = b.pr_number
              WHERE b.repo_full_name = :repo
                AND b.pr_number IS NOT NULL
                AND b.normalized_build_key IS NOT NULL
                {_optional_clause(filters.job_name, "AND b.job_name = :job_name")}
                {_optional_clause(filters.cloud_phase, "AND UPPER(COALESCE(NULLIF(b.cloud_phase, ''), 'IDC')) = :cloud_phase")}
                {_optional_clause(filters.start_date, "AND b.start_time >= :start_time_from")}
                {_optional_clause(filters.end_date, "AND b.start_time < :start_time_to")}
            ),
            case_runs_raw AS (
              SELECT
                pcr.branch,
                pcr.case_name,
                {_normalize_case_build_key_expr(connection, "pcr.build_url")} AS build_key,
                {_case_cloud_phase_expr("pcr.build_url")} AS cloud_phase,
                pcr.report_time,
                CASE WHEN pcr.flaky = 1 THEN 1 ELSE 0 END AS flaky_flag
              FROM problem_case_runs pcr
              WHERE pcr.repo = :repo
                AND pcr.branch IS NOT NULL
                AND pcr.branch <> ''
                AND pcr.case_name IS NOT NULL
                AND pcr.case_name <> ''
                {_optional_clause(filters.branch, "AND pcr.branch = :branch")}
                {_optional_clause(filters.start_date, "AND pcr.report_time >= :case_report_time_from")}
                {_optional_clause(filters.end_date, "AND pcr.report_time < :case_report_time_to")}
            ),
            case_runs AS (
              SELECT
                branch,
                case_name,
                build_key,
                cloud_phase,
                report_time,
                MAX(flaky_flag) AS flaky_flag
              FROM case_runs_raw
              GROUP BY branch, case_name, build_key, cloud_phase, report_time
            )
            SELECT DISTINCT
              bs.branch,
              bs.week_start,
              cr.case_name
            FROM build_scope bs
            JOIN case_runs cr
              ON cr.build_key = bs.normalized_build_key
             AND cr.branch = bs.branch
             AND cr.cloud_phase = bs.cloud_phase
             AND {_case_build_time_match_expr(connection, "cr.report_time", "bs.start_time")}
            WHERE cr.flaky_flag = 1
            ORDER BY bs.branch, cr.case_name, bs.week_start
            """
        ),
        _distinct_case_params(filters, effective_repo),
    ).mappings()
    return [dict(row) for row in rows]


def _build_issue_scope_ctes(
    filters: CommonFilters,
    effective_repo: str,
    *,
    include_issue_status: bool = True,
) -> tuple[str, dict[str, Any]]:
    conditions = [
        "fi.repo = :repo",
        "fi.case_name IS NOT NULL",
        "fi.case_name <> ''",
    ]
    params: dict[str, Any] = {"repo": effective_repo}

    if filters.branch:
        conditions.append("fi.issue_branch = :branch")
        params["branch"] = filters.branch
    else:
        conditions.append("fi.issue_branch IS NOT NULL")
        conditions.append("fi.issue_branch <> ''")

    if include_issue_status and filters.issue_status:
        conditions.append("LOWER(fi.issue_status) = :issue_status")
        params["issue_status"] = filters.issue_status.lower()

    if filters.end_date:
        conditions.append("fi.issue_created_at < :issue_window_end")
        params["issue_window_end"] = filters.end_date + timedelta(days=1)

    if filters.start_date:
        conditions.append(
            "(LOWER(fi.issue_status) <> 'closed' OR fi.issue_closed_at IS NULL OR fi.issue_closed_at >= :issue_window_start)"
        )
        params["issue_window_start"] = filters.start_date

    where_clause = " AND ".join(conditions)
    ctes = f"""
        ranked_issues AS (
          SELECT
            fi.repo,
            fi.issue_number,
            fi.issue_url,
            fi.issue_title,
            fi.case_name,
            fi.issue_status,
            fi.issue_branch,
            fi.issue_created_at,
            fi.issue_closed_at,
            fi.last_reopened_at,
            fi.reopen_count,
            ROW_NUMBER() OVER (
              PARTITION BY fi.repo, COALESCE(NULLIF(fi.issue_branch, ''), '{UNKNOWN_ISSUE_BRANCH}'), fi.case_name
              ORDER BY fi.issue_created_at DESC, fi.issue_number DESC
            ) AS rn
          FROM ci_l1_flaky_issues fi
          WHERE {where_clause}
        ),
        issue_cases AS (
          SELECT
            repo,
            issue_number,
            issue_url,
            issue_title,
            case_name,
            issue_status,
            issue_branch,
            issue_created_at,
            issue_closed_at,
            last_reopened_at,
            reopen_count
          FROM ranked_issues
          WHERE rn = 1
        )
    """
    return ctes, params


def _fetch_issue_latest_rows(
    connection: Connection,
    filters: CommonFilters,
    effective_repo: str,
) -> list[dict[str, Any]]:
    conditions = [
        "fi.repo = :repo",
        "fi.issue_number IS NOT NULL",
    ]
    params: dict[str, Any] = {"repo": effective_repo}

    if filters.branch:
        conditions.append("fi.issue_branch = :branch")
        params["branch"] = filters.branch
    else:
        conditions.append("fi.issue_branch IS NOT NULL")
        conditions.append("fi.issue_branch <> ''")

    rows = connection.execute(
        text(
            f"""
            WITH ranked_issues AS (
              SELECT
                fi.*,
                ROW_NUMBER() OVER (
                  PARTITION BY fi.repo, fi.issue_number
                  ORDER BY fi.issue_updated_at DESC, fi.updated_at DESC, fi.id DESC
                ) AS rn
              FROM ci_l1_flaky_issues fi
              WHERE {' AND '.join(conditions)}
            )
            SELECT
              repo,
              issue_number,
              issue_status,
              issue_branch,
              issue_created_at,
              issue_closed_at,
              last_reopened_at,
              reopen_count
            FROM ranked_issues
            WHERE rn = 1
            """
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def _latest_complete_week_start(end_date: date | None) -> date | None:
    if end_date is None:
        return None
    week_start = end_date - timedelta(days=end_date.weekday())
    if end_date >= week_start + timedelta(days=6):
        return week_start
    return week_start - timedelta(days=7)


def _parse_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _week_start_iso_from_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    week_start = value.date() - timedelta(days=value.date().weekday())
    return week_start.isoformat()


def _issue_overlaps_window(
    row: dict[str, Any],
    *,
    start_date: date | None,
    end_date: date | None,
) -> bool:
    created_at = _parse_datetime_value(row.get("issue_created_at"))
    if created_at is None:
        return False

    start_dt = datetime.combine(start_date, datetime.min.time()) if start_date else None
    end_exclusive_dt = (
        datetime.combine(end_date + timedelta(days=1), datetime.min.time())
        if end_date
        else None
    )
    closed_at = _parse_datetime_value(row.get("issue_closed_at"))
    status = str(row.get("issue_status") or "").lower()

    if end_exclusive_dt and created_at >= end_exclusive_dt:
        return False
    if start_dt:
        if status != "closed" or closed_at is None:
            return True
        return closed_at >= start_dt
    return True


def _build_build_where(
    filters: CommonFilters,
    *,
    table_alias: str,
) -> tuple[str, dict[str, Any]]:
    conditions = ["1=1"]
    params: dict[str, Any] = {}
    prefix = f"{table_alias}."

    if filters.repo:
        conditions.append(f"{prefix}repo_full_name = :repo")
        params["repo"] = filters.repo

    if filters.branch:
        conditions.append(branch_match_expr(table_alias))
        params["branch"] = filters.branch

    if filters.job_name:
        conditions.append(f"{prefix}job_name = :job_name")
        params["job_name"] = filters.job_name

    if filters.cloud_phase:
        conditions.append(f"{prefix}cloud_phase = :cloud_phase")
        params["cloud_phase"] = filters.cloud_phase

    if filters.start_date:
        conditions.append(f"{prefix}start_time >= :start_time_from")
        params["start_time_from"] = filters.start_date

    if filters.end_date:
        conditions.append(f"{prefix}start_time < :start_time_to")
        params["start_time_to"] = filters.end_date + timedelta(days=1)

    return " AND ".join(conditions), params


def _distinct_case_params(filters: CommonFilters, effective_repo: str) -> dict[str, Any]:
    params: dict[str, Any] = {"repo": effective_repo}
    if filters.branch:
        params["branch"] = filters.branch
    if filters.job_name:
        params["job_name"] = filters.job_name
    if filters.cloud_phase:
        params["cloud_phase"] = filters.cloud_phase.upper()
    if filters.start_date:
        params["start_time_from"] = filters.start_date
        params["case_report_time_from"] = filters.start_date
    if filters.end_date:
        params["start_time_to"] = filters.end_date + timedelta(days=1)
        params["case_report_time_to"] = filters.end_date + timedelta(days=2)
    return params


def _distinct_pr_key_expr(connection: Connection, table_alias: str) -> str:
    prefix = f"{table_alias}."
    if connection.dialect.name == "sqlite":
        return f"{prefix}repo_full_name || '#' || CAST({prefix}pr_number AS TEXT)"
    return f"CONCAT({prefix}repo_full_name, '#', CAST({prefix}pr_number AS CHAR))"


def _optional_clause(value: object | None, clause: str) -> str:
    return clause if value is not None else ""


def _normalize_case_build_key_expr(connection: Connection, column_name: str) -> str:
    stripped = (
        "REPLACE(REPLACE(REPLACE("
        f"{column_name}, "
        "'https://do.pingcap.net', ''), "
        "'https://prow.tidb.net', ''), "
        "'/display/redirect', '')"
    )
    if connection.dialect.name == "sqlite":
        return f"RTRIM({stripped}, '/')"
    return f"TRIM(TRAILING '/' FROM {stripped})"


def _case_cloud_phase_expr(column_name: str) -> str:
    return (
        "CASE "
        f"WHEN COALESCE({column_name}, '') LIKE 'https://prow.tidb.net/jenkins/%' THEN 'GCP' "
        "ELSE 'IDC' "
        "END"
    )


def _case_build_time_match_expr(connection: Connection, report_time_column: str, start_time_column: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"{report_time_column} BETWEEN {start_time_column} AND datetime({start_time_column}, '+24 hours')"
    return f"{report_time_column} BETWEEN {start_time_column} AND {start_time_column} + INTERVAL 24 HOUR"


def _week_columns(
    start_date: date | None,
    end_date: date | None,
    rows: list[dict[str, Any]],
) -> list[str]:
    if start_date and end_date:
        current = start_date - timedelta(days=start_date.weekday())
        last = end_date - timedelta(days=end_date.weekday())
        weeks: list[str] = []
        while current <= last:
            weeks.append(current.isoformat())
            current += timedelta(days=7)
        return weeks
    return sorted({str(row["week_start"]) for row in rows if row.get("week_start") is not None})


def _ordered_branches(branches: Any, selected_branch: str | None) -> list[str]:
    if selected_branch:
        return [selected_branch]

    preferred_order = {"master": 0, "release-8.5": 1}
    return sorted(set(branches), key=lambda item: (preferred_order.get(item, 99), item))


def _issue_case_key(*, issue_branch: Any, case_name: Any) -> tuple[str, str]:
    return (str(issue_branch or ""), str(case_name))


def _issue_display_name(
    *,
    case_name: str,
    issue_branch: Any,
    selected_branch: str | None,
) -> str:
    if selected_branch or not issue_branch:
        return case_name
    return f"[{issue_branch}] {case_name}"


def _build_rate_metric(
    *,
    week_start: str,
    flaky_runs: int,
    total_runs_est: int,
) -> dict[str, Any]:
    flaky_rate_pct = rate_pct(flaky_runs, total_runs_est)
    return {
        "week_start": week_start,
        "flaky_runs": flaky_runs,
        "total_runs_est": total_runs_est,
        "flaky_rate_pct": flaky_rate_pct,
        "cell": f"{flaky_rate_pct:.2f}% ({flaky_runs}/{total_runs_est})",
    }
