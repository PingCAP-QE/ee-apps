from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.refresh_build_derived import (
    _execute_statement_in_batches,
    _fetch_group_builds_for_groups,
    _resolve_refresh_group_chunk_size,
    run_refresh_flaky_signals_for_time_window,
    run_refresh_build_derived,
    run_refresh_build_derived_for_time_window,
)
from ci_dashboard.jobs.state_store import get_job_state


def _settings(*, batch_size: int = 100, refresh_build_limit: int = 5000) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(
            batch_size=batch_size,
            refresh_build_limit=refresh_build_limit,
        ),
        log_level="INFO",
    )


def _insert_build(
    sqlite_engine,
    *,
    source_prow_job_id: str,
    repo_full_name: str = "pingcap/tidb",
    pr_number: int = 101,
    job_name: str = "unit-test",
    state: str = "failure",
    head_sha: str = "sha-1",
    normalized_build_key: str | None = None,
    start_time: str = "2026-04-13 10:00:00",
) -> int:
    org, repo = repo_full_name.split("/", 1)
    with sqlite_engine.begin() as connection:
        result = connection.execute(
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
                  :source_prow_row_id, :source_prow_job_id, 'prow', :job_name, 'presubmit', :state,
                  0, 1, :org, :repo, :repo_full_name, 'master', :pr_number, 1,
                  'unit-test', 'https://prow.tidb.net/jenkins/job/x/1/display/redirect',
                  :normalized_build_key, 'alice', 0, 'guid', '1', NULL, NULL, :start_time,
                  :start_time, 0, 0, 0, :head_sha, NULL, 'GCP', 0, 0, 0, NULL, NULL
                )
                """
            ),
            {
                "source_prow_row_id": int(source_prow_job_id.split("-")[-1]),
                "source_prow_job_id": source_prow_job_id,
                "job_name": job_name,
                "state": state,
                "org": org,
                "repo": repo,
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "normalized_build_key": normalized_build_key or f"/jenkins/job/{source_prow_job_id}",
                "head_sha": head_sha,
                "start_time": start_time,
            },
        )
        return int(result.lastrowid)


def _insert_pr_snapshot(
    sqlite_engine,
    *,
    repo: str = "pingcap/tidb",
    pr_number: int = 101,
    target_branch: str = "release-8.5",
    updated_at: str = "2026-04-13 11:00:00",
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pr_events (
                  repo, pr_number, event_key, event_time, event_type, actor_login, comment_id,
                  comment_body, retest_event, commit_sha, target_branch, head_ref, head_sha,
                  created_at, updated_at
                ) VALUES (
                  :repo, :pr_number, 'pr_snapshot', :updated_at, 'pr_snapshot', NULL, NULL,
                  NULL, 0, NULL, :target_branch, 'feature-x', 'sha-1', :updated_at, :updated_at
                )
                """
            ),
            {
                "repo": repo,
                "pr_number": pr_number,
                "target_branch": target_branch,
                "updated_at": updated_at,
            },
        )


def _insert_problem_case_run(
    sqlite_engine,
    *,
    repo: str = "pingcap/tidb",
    build_url: str,
    report_time: str,
    flaky: int = 1,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO problem_case_runs (
                  repo, branch, suite_name, case_name, flaky, timecost_ms, report_time, build_url, reason
                ) VALUES (
                  :repo, 'master', 'unit', 'case-a', :flaky, 10, :report_time, :build_url, 'flake'
                )
                """
            ),
            {
                "repo": repo,
                "flaky": flaky,
                "report_time": report_time,
                "build_url": build_url,
            },
        )


def test_execute_statement_in_batches_splits_large_payload() -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, int]]] = []

        def execute(self, statement, payload) -> None:
            self.calls.append(list(payload))

    connection = FakeConnection()
    statement = text("UPDATE ci_l1_builds SET is_flaky = :is_flaky WHERE id = :id")
    payload = [{"id": index, "is_flaky": 0} for index in range(5)]

    _execute_statement_in_batches(connection, statement, payload, batch_size=2)

    assert [len(call) for call in connection.calls] == [2, 2, 1]


def test_refresh_group_chunk_size_caps_flaky_group_transactions() -> None:
    assert _resolve_refresh_group_chunk_size(_settings(batch_size=200)) == 25
    assert _resolve_refresh_group_chunk_size(_settings(batch_size=20)) == 20


def test_fetch_group_builds_for_groups_batches_and_buckets_rows(sqlite_engine) -> None:
    first_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-100",
        pr_number=101,
        job_name="unit-test",
        head_sha="sha-100",
        start_time="2026-04-13 10:00:00",
    )
    second_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-101",
        pr_number=101,
        job_name="unit-test",
        head_sha="sha-101",
        start_time="2026-04-13 10:05:00",
    )
    third_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-102",
        pr_number=102,
        job_name="integration-test",
        head_sha="sha-102",
        start_time="2026-04-13 11:00:00",
    )
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-103",
        pr_number=102,
        job_name="integration-test",
        head_sha="",
        start_time="2026-04-13 11:05:00",
    )

    with sqlite_engine.begin() as connection:
        builds_by_group = _fetch_group_builds_for_groups(
            connection,
            [
                {"repo_full_name": "pingcap/tidb", "pr_number": 101, "job_name": "unit-test"},
                {"repo_full_name": "pingcap/tidb", "pr_number": 102, "job_name": "integration-test"},
            ],
        )

    assert sorted(builds_by_group) == [
        ("pingcap/tidb", 101, "unit-test"),
        ("pingcap/tidb", 102, "integration-test"),
    ]
    assert [int(row["id"]) for row in builds_by_group[("pingcap/tidb", 101, "unit-test")]] == [
        first_id,
        second_id,
    ]
    assert [
        int(row["id"]) for row in builds_by_group[("pingcap/tidb", 102, "integration-test")]
    ] == [third_id]


def test_refresh_build_derived_end_to_end(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-1",
        head_sha="sha-1",
        state="failure",
        normalized_build_key="/jenkins/job/prow-job-1",
        start_time="2026-04-13 10:00:00",
    )
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-2",
        head_sha="sha-1",
        state="failure",
        normalized_build_key="/jenkins/job/prow-job-2",
        start_time="2026-04-13 10:05:00",
    )
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-3",
        head_sha="sha-1",
        state="success",
        normalized_build_key="/jenkins/job/prow-job-3",
        start_time="2026-04-13 10:10:00",
    )
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-4",
        pr_number=102,
        head_sha="sha-retry-1",
        state="failure",
        normalized_build_key="/jenkins/job/prow-job-4",
        start_time="2026-04-13 11:00:00",
    )
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-5",
        pr_number=102,
        head_sha="sha-retry-1",
        state="failure",
        normalized_build_key="/jenkins/job/prow-job-5",
        start_time="2026-04-13 11:05:00",
    )
    next_sha = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-6",
        pr_number=102,
        head_sha="sha-retry-2",
        state="failure",
        normalized_build_key="/jenkins/job/prow-job-6",
        start_time="2026-04-13 11:10:00",
    )

    _insert_pr_snapshot(sqlite_engine, pr_number=101, target_branch="release-8.5")
    _insert_pr_snapshot(sqlite_engine, pr_number=102, target_branch="release-8.6")
    _insert_problem_case_run(
        sqlite_engine,
        build_url="https://prow.tidb.net/jenkins/job/prow-job-1/display/redirect",
        report_time="2026-04-13 10:15:00",
    )

    summary = run_refresh_build_derived(sqlite_engine, _settings(batch_size=2))

    assert summary.impacted_builds == 6
    assert summary.groups_recomputed == 2
    assert summary.branch_rows_updated == 6
    assert summary.flaky_rows_updated == 6
    assert summary.case_match_rows_updated == 1
    assert summary.failure_category_rows_updated == 6

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT id, pr_number, target_branch, is_flaky, is_retry_loop, has_flaky_case_match, failure_category
                    FROM ci_l1_builds
                    ORDER BY id
                    """
                )
            ).mappings()
        )
        state = get_job_state(connection, "ci-refresh-build-derived")

    assert rows[0]["target_branch"] == "release-8.5"
    assert rows[1]["target_branch"] == "release-8.5"
    assert rows[0]["is_flaky"] == 1
    assert rows[1]["is_flaky"] == 1
    assert rows[2]["is_flaky"] == 0
    assert rows[0]["has_flaky_case_match"] == 1
    assert rows[0]["failure_category"] == "FLAKY_TEST"
    assert rows[1]["failure_category"] == "FLAKY_TEST"
    assert rows[2]["failure_category"] is None
    assert rows[3]["is_retry_loop"] == 0
    assert rows[4]["is_retry_loop"] == 1
    assert rows[4]["failure_category"] == "FLAKY_TEST"
    assert rows[5]["is_retry_loop"] == 0
    assert state is not None
    assert state.last_status == "succeeded"
    assert state.watermark["last_processed_build_id"] == next_sha


def test_refresh_build_derived_slices_large_backlog_and_freezes_selection_window(sqlite_engine) -> None:
    first_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-200",
        normalized_build_key="/jenkins/job/prow-job-200",
        start_time="2026-04-13 09:00:00",
    )
    second_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-201",
        normalized_build_key="/jenkins/job/prow-job-201",
        start_time="2026-04-13 09:05:00",
    )
    third_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-202",
        normalized_build_key="/jenkins/job/prow-job-202",
        start_time="2026-04-13 09:10:00",
    )
    _insert_pr_snapshot(
        sqlite_engine,
        pr_number=101,
        target_branch="master",
        updated_at="2026-04-13 09:30:00",
    )

    settings = _settings(batch_size=10, refresh_build_limit=2)

    first_summary = run_refresh_build_derived(sqlite_engine, settings)
    assert first_summary.impacted_builds == 2

    fourth_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-203",
        normalized_build_key="/jenkins/job/prow-job-203",
        start_time="2026-04-13 09:15:00",
    )

    second_summary = run_refresh_build_derived(sqlite_engine, settings)
    assert second_summary.impacted_builds == 1

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT id, target_branch
                    FROM ci_l1_builds
                    WHERE id IN (:first_id, :second_id, :third_id, :fourth_id)
                    ORDER BY id
                    """
                ),
                {
                    "first_id": first_id,
                    "second_id": second_id,
                    "third_id": third_id,
                    "fourth_id": fourth_id,
                },
            ).mappings()
        )
        state_after_second_run = get_job_state(connection, "ci-refresh-build-derived")

    assert [row["target_branch"] for row in rows[:3]] == ["master", "master", "master"]
    assert rows[3]["target_branch"] is None
    assert state_after_second_run is not None
    assert state_after_second_run.watermark["pending_refresh"] is False
    assert state_after_second_run.watermark["last_processed_build_id"] == third_id

    third_summary = run_refresh_build_derived(sqlite_engine, settings)
    assert third_summary.impacted_builds == 1

    with sqlite_engine.begin() as connection:
        final_row = connection.execute(
            text(
                """
                SELECT target_branch
                FROM ci_l1_builds
                WHERE id = :id
                """
            ),
            {"id": fourth_id},
        ).mappings().one()
        final_state = get_job_state(connection, "ci-refresh-build-derived")

    assert final_row["target_branch"] == "master"
    assert final_state is not None
    assert final_state.watermark["last_processed_build_id"] == fourth_id


def test_refresh_build_derived_picks_up_new_case_rows_incrementally(sqlite_engine) -> None:
    build_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-20",
        normalized_build_key="/jenkins/job/prow-job-20",
        start_time="2026-04-13 13:00:00",
    )
    _insert_pr_snapshot(sqlite_engine, pr_number=101, target_branch="master", updated_at="2026-04-13 13:30:00")

    first_summary = run_refresh_build_derived(sqlite_engine, _settings())
    assert first_summary.case_match_rows_updated == 0

    _insert_problem_case_run(
        sqlite_engine,
        build_url="https://prow.tidb.net/jenkins/job/prow-job-20/display/redirect",
        report_time="2026-04-13 13:20:00",
    )

    second_summary = run_refresh_build_derived(sqlite_engine, _settings())
    assert second_summary.impacted_builds == 1
    assert second_summary.case_match_rows_updated == 1

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT has_flaky_case_match
                FROM ci_l1_builds
                WHERE id = :id
                """
            ),
            {"id": build_id},
        ).fetchone()

    assert row is not None
    assert row[0] == 1


def test_refresh_build_derived_time_window_is_repeatable(sqlite_engine) -> None:
    in_window_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-30",
        normalized_build_key="/jenkins/job/prow-job-30",
        start_time="2026-04-13 10:00:00",
    )
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-31",
        pr_number=202,
        normalized_build_key="/jenkins/job/prow-job-31",
        start_time="2026-04-12 23:59:59",
    )
    _insert_pr_snapshot(sqlite_engine, pr_number=101, target_branch="master", updated_at="2026-04-13 11:00:00")
    _insert_problem_case_run(
        sqlite_engine,
        build_url="https://prow.tidb.net/jenkins/job/prow-job-30/display/redirect",
        report_time="2026-04-13 10:15:00",
    )

    first_summary = run_refresh_build_derived_for_time_window(
        sqlite_engine,
        _settings(),
        start_time_from=datetime(2026, 4, 13, 0, 0, 0),
        start_time_to=datetime(2026, 4, 14, 0, 0, 0),
    )
    second_summary = run_refresh_build_derived_for_time_window(
        sqlite_engine,
        _settings(),
        start_time_from=datetime(2026, 4, 13, 0, 0, 0),
        start_time_to=datetime(2026, 4, 14, 0, 0, 0),
    )

    assert first_summary.impacted_builds == 1
    assert second_summary.impacted_builds == 1

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT target_branch, has_flaky_case_match, failure_category
                FROM ci_l1_builds
                WHERE id = :id
                """
            ),
            {"id": in_window_id},
        ).mappings().one()
        state = get_job_state(connection, "ci-refresh-build-derived")

    assert row["target_branch"] == "master"
    assert row["has_flaky_case_match"] == 1
    assert row["failure_category"] is None
    assert state is None


def test_refresh_flaky_signals_time_window_skips_branch_and_case_phases(sqlite_engine) -> None:
    first_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-40",
        normalized_build_key="/jenkins/job/prow-job-40",
        start_time="2026-04-13 10:00:00",
        state="failure",
    )
    second_id = _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-41",
        normalized_build_key="/jenkins/job/prow-job-41",
        start_time="2026-04-13 10:05:00",
        state="failure",
    )
    _insert_build(
        sqlite_engine,
        source_prow_job_id="prow-job-42",
        normalized_build_key="/jenkins/job/prow-job-42",
        start_time="2026-04-13 10:10:00",
        state="success",
    )
    _insert_pr_snapshot(sqlite_engine, pr_number=101, target_branch="release-8.5")
    _insert_problem_case_run(
        sqlite_engine,
        build_url="https://prow.tidb.net/jenkins/job/prow-job-40/display/redirect",
        report_time="2026-04-13 10:15:00",
    )

    summary = run_refresh_flaky_signals_for_time_window(
        sqlite_engine,
        _settings(batch_size=50),
        start_time_from=datetime(2026, 4, 13, 0, 0, 0),
        start_time_to=datetime(2026, 4, 14, 0, 0, 0),
    )

    assert summary.impacted_builds == 3
    assert summary.groups_recomputed == 1
    assert summary.branch_rows_updated == 0
    assert summary.case_match_rows_updated == 0
    assert summary.failure_category_rows_updated == 3

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT id, target_branch, is_flaky, has_flaky_case_match, failure_category
                    FROM ci_l1_builds
                    WHERE id IN (:first_id, :second_id)
                    ORDER BY id
                    """
                ),
                {"first_id": first_id, "second_id": second_id},
            ).mappings()
        )

    assert rows[0]["target_branch"] is None
    assert rows[0]["is_flaky"] == 1
    assert rows[0]["has_flaky_case_match"] == 0
    assert rows[0]["failure_category"] == "FLAKY_TEST"
    assert rows[1]["target_branch"] is None
    assert rows[1]["is_flaky"] == 1
