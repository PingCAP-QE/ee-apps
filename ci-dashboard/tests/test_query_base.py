from __future__ import annotations

from sqlalchemy import text

from ci_dashboard.api.queries.base import CommonFilters, build_common_where


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
                  context, url, normalized_build_key, author, retest, event_guid, build_id,
                  pod_name, pending_time, start_time, completion_time, queue_wait_seconds,
                  run_seconds, total_seconds, head_sha, target_branch, cloud_phase, is_flaky,
                  is_retry_loop, has_flaky_case_match, failure_category, failure_subcategory
                ) VALUES (
                  :source_prow_row_id, :source_prow_job_id, 'prow', 'unit-test', 'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', :base_ref, 100, 1,
                  'unit-test', :url, :normalized_build_key, 'alice', 0, 'guid', '1',
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
                "normalized_build_key": f"/jenkins/job/{source_prow_job_id}",
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
