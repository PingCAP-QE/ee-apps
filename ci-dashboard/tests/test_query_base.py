from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import text

from ci_dashboard.api.queries.base import (
    CommonFilters,
    branch_expr,
    bucket_expr,
    builds_table_expr,
    complete_week_bounds,
    filter_complete_week_rows,
    isoformat_utc,
    rate_pct,
    to_number,
    build_common_where,
)


def _insert_build(
    sqlite_engine,
    *,
    source_prow_row_id: int,
    source_prow_job_id: str,
    target_branch: str | None,
    base_ref: str | None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, base_ref, pr_number, is_pr_build,
                  context, url, normalized_build_url, author, retest, event_guid, build_id,
                  pod_name, pending_time, start_time, completion_time, queue_wait_seconds,
                  run_seconds, total_seconds, head_sha, target_branch, cloud_phase, is_flaky,
                  is_retry_loop, has_flaky_case_match, failure_category, failure_subcategory
                ) VALUES (
                  :source_prow_row_id, :source_prow_job_id, 'prow', 'unit-test', 'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', :base_ref, 100, 1,
                  'unit-test', :url, :normalized_build_url, 'alice', 0, 'guid', '1',
                  NULL, NULL, '2026-04-20T00:00:00Z', '2026-04-20T00:00:00Z', 0,
                  0, 0, 'sha', :target_branch, 'GCP', 0,
                  0, 0, NULL, NULL
                )
                """
            ),
            {
                "source_prow_row_id": source_prow_row_id,
                "source_prow_job_id": source_prow_job_id,
                "base_ref": base_ref,
                "url": f"https://prow.tidb.net/jenkins/job/{source_prow_job_id}/display/redirect",
                "normalized_build_url": f"https://prow.tidb.net/jenkins/job/{source_prow_job_id}/",
                "target_branch": target_branch,
            },
        )


def test_build_common_where_branch_match_preserves_target_branch_fallback(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        source_prow_row_id=1,
        source_prow_job_id="job-target-match",
        target_branch="master",
        base_ref="release-8.5",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=2,
        source_prow_job_id="job-null-target",
        target_branch=None,
        base_ref="master",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=3,
        source_prow_job_id="job-empty-target",
        target_branch="",
        base_ref="master",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=4,
        source_prow_job_id="job-nonmatching-target",
        target_branch="main",
        base_ref="master",
    )

    where_clause, params = build_common_where(
        CommonFilters(branch="master"),
        table_alias="b",
    )

    with sqlite_engine.begin() as connection:
        matched_job_ids = connection.execute(
            text(
                f"""
                SELECT source_prow_job_id
                FROM ci_l1_builds b
                WHERE {where_clause}
                ORDER BY source_prow_row_id
                """
            ),
            params,
        ).scalars().all()

    assert matched_job_ids == [
        "job-target-match",
        "job-null-target",
        "job-empty-target",
    ]


def test_build_common_where_skips_branch_predicate_for_empty_branch() -> None:
    where_clause, params = build_common_where(
        CommonFilters(branch=""),
        table_alias="b",
    )

    assert where_clause == "1=1"
    assert params == {}


def test_build_common_where_supports_job_repo_cloud_and_date_filters() -> None:
    where_clause, params = build_common_where(
        CommonFilters(
            repo="pingcap/tidb",
            branch="master",
            job_name="ghpr_unit_test",
            cloud_phase="GCP",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 7),
        ),
        table_alias="b",
    )

    assert "b.repo_full_name = :repo" in where_clause
    assert "b.job_name = :job_name" in where_clause
    assert "b.cloud_phase = :cloud_phase" in where_clause
    assert "b.start_time >= :start_time_from" in where_clause
    assert "b.start_time < :start_time_to" in where_clause
    assert params["repo"] == "pingcap/tidb"
    assert params["job_name"] == "ghpr_unit_test"
    assert params["cloud_phase"] == "GCP"
    assert params["start_time_from"] == datetime(2026, 4, 1, 0, 0, 0)
    assert params["start_time_to"] == datetime(2026, 4, 8, 0, 0, 0)


def test_query_base_helpers_cover_tidb_and_week_filters() -> None:
    sqlite_connection = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    tidb_connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    assert branch_expr("b") == "COALESCE(NULLIF(b.target_branch, ''), b.base_ref)"
    assert bucket_expr(sqlite_connection, "start_time", "day") == "DATE(start_time)"
    assert "strftime('%w'" in bucket_expr(sqlite_connection, "start_time", "week")
    assert bucket_expr(tidb_connection, "start_time", "week") == "DATE_SUB(DATE(start_time), INTERVAL WEEKDAY(start_time) DAY)"

    assert builds_table_expr(sqlite_connection, CommonFilters(repo="pingcap/tidb")) == "ci_l1_builds b"
    assert (
        builds_table_expr(
            tidb_connection,
            CommonFilters(repo="pingcap/tidb", start_date=date(2026, 4, 1)),
        )
        == "ci_l1_builds b FORCE INDEX(idx_ci_l1_builds_repo_time)"
    )
    assert (
        builds_table_expr(
            tidb_connection,
            CommonFilters(start_date=date(2026, 4, 1)),
        )
        == "ci_l1_builds b FORCE INDEX(idx_ci_l1_builds_start_time_id)"
    )
    assert builds_table_expr(tidb_connection, CommonFilters()) == "ci_l1_builds b"


def test_week_filters_and_numeric_helpers_cover_edge_cases() -> None:
    rows = [
        {"bucket_start": "2026-04-06", "value": 1},
        {"bucket_start": "2026-04-13", "value": 2},
        {"bucket_start": "not-a-date", "value": 3},
        {"bucket_start": None, "value": 4},
    ]

    assert complete_week_bounds(date(2026, 4, 8), date(2026, 4, 10)) == (None, None)
    assert complete_week_bounds(date(2026, 4, 6), date(2026, 4, 19)) == (
        date(2026, 4, 6),
        date(2026, 4, 13),
    )
    assert filter_complete_week_rows(rows, start_date=None, end_date=date(2026, 4, 19)) == rows
    assert filter_complete_week_rows(rows, start_date=date(2026, 4, 8), end_date=date(2026, 4, 10)) == []
    assert filter_complete_week_rows(rows, start_date=date(2026, 4, 6), end_date=date(2026, 4, 19)) == [
        {"bucket_start": "2026-04-06", "value": 1},
        {"bucket_start": "2026-04-13", "value": 2},
    ]

    assert to_number(Decimal("12")) == 12
    assert to_number(Decimal("12.5")) == 12.5
    assert to_number(4.0) == 4
    assert to_number(4.567) == 4.57
    assert to_number("7") == 7
    assert to_number("7.129") == 7.13
    assert to_number("not-a-number") is None
    assert rate_pct(1, 4) == 25.0
    assert rate_pct(1, 0) == 0.0
    assert isoformat_utc(None) is None
    assert isoformat_utc(datetime(2026, 4, 1, 0, 0, 0)) == "2026-04-01T00:00:00Z"
    assert (
        isoformat_utc(datetime(2026, 4, 1, 8, 0, 0, tzinfo=timezone.utc))
        == "2026-04-01T08:00:00Z"
    )
