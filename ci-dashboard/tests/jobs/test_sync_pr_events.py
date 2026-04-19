from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.state_store import get_job_state
from ci_dashboard.jobs.sync_pr_events import (
    _extract_branch_metadata,
    _hash_key,
    _parse_timeline,
    run_sync_pr_events,
    run_sync_pr_events_for_time_window,
)


def _settings(batch_size: int = 100) -> Settings:
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
        jobs=JobSettings(batch_size=batch_size),
        log_level="INFO",
    )


def _insert_build(
    sqlite_engine,
    *,
    source_prow_row_id: int = 1,
    source_prow_job_id: str = "prow-job-1",
    repo_full_name: str = "pingcap/tidb",
    pr_number: int = 101,
) -> None:
    org, repo = repo_full_name.split("/", 1)
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
                  :source_prow_row_id, :source_prow_job_id, 'prow', 'unit-test', 'presubmit', 'failure',
                  0, 1, :org, :repo, :repo_full_name, 'master', :pr_number, 1,
                  'unit-test', 'https://prow.tidb.net/jenkins/job/x/1/display/redirect',
                  '/jenkins/job/x/1', 'alice', 0, 'guid', '1', NULL, NULL, '2026-04-13 10:00:00',
                  '2026-04-13 10:10:00', 10, 600, 610, 'abc123', NULL, 'GCP', 0, 0, 0, NULL, NULL
                )
                """
            ),
            {
                "source_prow_row_id": source_prow_row_id,
                "source_prow_job_id": source_prow_job_id,
                "org": org,
                "repo": repo,
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
            },
        )


def _insert_ticket(
    sqlite_engine,
    *,
    repo: str = "pingcap/tidb",
    number: int = 101,
    created_at: str = "2026-04-13 08:00:00",
    updated_at: str = "2026-04-13 11:00:00",
    timeline: list[dict] | None = None,
    branches: dict | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO github_tickets (
                  type, repo, number, created_at, updated_at, timeline, branches
                ) VALUES (
                  'pull', :repo, :number, :created_at, :updated_at, :timeline, :branches
                )
                """
            ),
            {
                "repo": repo,
                "number": number,
                "created_at": created_at,
                "updated_at": updated_at,
                "timeline": json.dumps(timeline or []),
                "branches": json.dumps(branches or {}),
            },
        )


def test_sync_pr_events_end_to_end(sqlite_engine) -> None:
    _insert_build(sqlite_engine)
    _insert_ticket(
        sqlite_engine,
        timeline=[
            {
                "event": "committed",
                "sha": "deadbeef",
                "author": {"name": "alice", "date": "2026-04-13T09:00:00Z"},
            },
            {
                "event": "commented",
                "id": 55,
                "body": "/retest",
                "created_at": "2026-04-13T09:05:00Z",
                "user": {"login": "bob"},
            },
            {
                "event": "commented",
                "id": 56,
                "body": "say /retest to rerun",
                "created_at": "2026-04-13T09:06:00Z",
                "user": {"login": "bot"},
            },
        ],
        branches={
            "base": {"ref": "release-8.5"},
            "head": {"ref": "feature-x", "sha": "deadbeef"},
        },
    )

    summary = run_sync_pr_events(sqlite_engine, _settings())

    assert summary.candidate_prs == 1
    assert summary.ticket_rows_fetched == 1
    assert summary.events_written == 3
    assert summary.last_build_source_prow_row_id_seen == 1
    assert summary.last_ticket_updated_at == "2026-04-13 11:00:00"

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT event_type, event_key, retest_event, actor_login, commit_sha, target_branch, head_ref, head_sha
                    FROM ci_l1_pr_events
                    ORDER BY event_time, id
                    """
                )
            ).mappings()
        )
        state = get_job_state(connection, "ci-sync-pr-events")

    assert {row["event_type"] for row in rows} == {
        "pr_snapshot",
        "committed",
        "retest_comment",
    }
    row_by_type = {row["event_type"]: row for row in rows}
    assert row_by_type["pr_snapshot"]["event_key"] == "pr_snapshot"
    assert row_by_type["pr_snapshot"]["target_branch"] == "release-8.5"
    assert row_by_type["pr_snapshot"]["head_ref"] == "feature-x"
    assert row_by_type["committed"]["commit_sha"] == "deadbeef"
    assert row_by_type["retest_comment"]["retest_event"] == 1
    assert row_by_type["retest_comment"]["actor_login"] == "bob"
    assert state is not None
    assert state.last_status == "succeeded"
    assert state.watermark == {
        "last_build_source_prow_row_id_seen": 1,
        "last_ticket_updated_at": "2026-04-13 11:00:00",
    }


def test_sync_pr_events_refreshes_tracked_prs_when_ticket_changes(sqlite_engine) -> None:
    _insert_build(sqlite_engine, source_prow_row_id=10, source_prow_job_id="prow-job-10")
    _insert_ticket(
        sqlite_engine,
        updated_at="2026-04-13 11:00:00",
        branches={"base": {"ref": "master"}, "head": {"ref": "feature-a", "sha": "aaa"}},
    )

    first_summary = run_sync_pr_events(sqlite_engine, _settings())
    assert first_summary.events_written == 1

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE github_tickets
                SET updated_at = '2026-04-13 12:00:00',
                    branches = :branches
                WHERE repo = 'pingcap/tidb' AND number = 101
                """
            ),
            {"branches": json.dumps({"base": {"ref": "release-8.6"}, "head": {"ref": "feature-a", "sha": "aaa"}})},
        )

    second_summary = run_sync_pr_events(sqlite_engine, _settings())
    assert second_summary.candidate_prs == 1
    assert second_summary.events_written == 1
    assert second_summary.last_ticket_updated_at == "2026-04-13 12:00:00"

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT target_branch
                FROM ci_l1_pr_events
                WHERE repo = 'pingcap/tidb' AND pr_number = 101 AND event_key = 'pr_snapshot'
                """
            )
        ).fetchone()

    assert row is not None
    assert row[0] == "release-8.6"


def test_extract_branch_metadata_and_timeline_helpers() -> None:
    branch_meta = _extract_branch_metadata(
        {
            "branches": {
                "base": {"ref": "master"},
                "head": {"ref": "feature-z", "sha": "cafebabe"},
            }
        }
    )

    events = _parse_timeline(
        [
            {
                "event": "committed",
                "sha": "cafebabe",
                "author": {"login": "alice", "date": "2026-04-13T09:00:00Z"},
            },
            {
                "event": "commented",
                "id": 8,
                "body": "/retest-required",
                "created_at": "2026-04-13T09:05:00Z",
                "user": {"login": "bob"},
            },
        ],
        "pingcap/tidb",
        10,
        branch_meta,
    )

    assert branch_meta == {
        "target_branch": "master",
        "head_ref": "feature-z",
        "head_sha": "cafebabe",
    }
    assert [event["event_type"] for event in events] == ["committed", "retest_comment"]
    assert _hash_key("a", "b") == _hash_key("a", "b")
    assert _hash_key("a", "b") != _hash_key("a", "c")


def test_sync_pr_events_time_window_is_repeatable(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        source_prow_row_id=1,
        source_prow_job_id="prow-job-before",
        pr_number=99,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=2,
        source_prow_job_id="prow-job-window",
        pr_number=101,
    )
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ci_l1_builds
                SET start_time = :start_time
                WHERE source_prow_job_id = :source_prow_job_id
                """
            ),
            [
                {
                    "source_prow_job_id": "prow-job-before",
                    "start_time": "2026-04-12 23:59:59",
                },
                {
                    "source_prow_job_id": "prow-job-window",
                    "start_time": "2026-04-13 10:00:00",
                },
            ],
        )
    _insert_ticket(
        sqlite_engine,
        repo="pingcap/tidb",
        number=101,
        timeline=[
            {
                "event": "committed",
                "sha": "deadbeef",
                "author": {"name": "alice", "date": "2026-04-13T09:00:00Z"},
            },
            {
                "event": "commented",
                "id": 55,
                "body": "/retest",
                "created_at": "2026-04-13T09:05:00Z",
                "user": {"login": "bob"},
            },
        ],
        branches={
            "base": {"ref": "release-8.5"},
            "head": {"ref": "feature-x", "sha": "deadbeef"},
        },
    )

    first_summary = run_sync_pr_events_for_time_window(
        sqlite_engine,
        _settings(),
        start_time_from=datetime(2026, 4, 13, 0, 0, 0),
        start_time_to=datetime(2026, 4, 14, 0, 0, 0),
    )
    second_summary = run_sync_pr_events_for_time_window(
        sqlite_engine,
        _settings(),
        start_time_from=datetime(2026, 4, 13, 0, 0, 0),
        start_time_to=datetime(2026, 4, 14, 0, 0, 0),
    )

    assert first_summary.candidate_prs == 1
    assert second_summary.candidate_prs == 1

    with sqlite_engine.begin() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM ci_l1_pr_events")
        ).scalar_one()
        state = get_job_state(connection, "ci-sync-pr-events")

    assert count == 3
    assert state is None
