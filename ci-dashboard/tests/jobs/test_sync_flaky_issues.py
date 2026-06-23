from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from urllib import error as urllib_error

import pytest
from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.state_store import get_job_state
from ci_dashboard.jobs.sync_flaky_issues import (
    _build_flaky_issue_row,
    _build_issue_pr_links_upsert_statement,
    _build_upsert_statement,
    _enrich_issue_row_for_pr_fallback,
    _extract_linked_pr_rows,
    _extract_issue_lifecycle,
    _fallback_issue_branch,
    _fetch_github_api_json,
    _normalize_issue_comments,
    _parse_case_name,
    _parse_datetime,
    _parse_linked_pr_candidates_from_body,
    _parse_timeline,
    _resolve_issue_branch,
    _reuse_existing_issue_branch_if_fresh,
    _upsert_flaky_issues,
    fetch_issue_details_via_github_api,
    parse_issue_branch,
    parse_issue_branch_from_comments,
    run_backfill_flaky_issue_pr_links,
    run_sync_flaky_issues,
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


def _insert_issue_ticket(
    sqlite_engine,
    *,
    ticket_id: int,
    repo: str,
    number: int,
    title: str,
    body: str | None = None,
    comments: list[dict[str, object]] | None = None,
    state: str,
    created_at: str,
    updated_at: str,
    timeline: list[dict[str, object]],
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO github_tickets (
                  id, type, repo, number, title, body, comments, state, created_at, updated_at, timeline, branches
                ) VALUES (
                  :ticket_id, 'issue', :repo, :number, :title, :body, :comments, :state, :created_at, :updated_at,
                  :timeline, NULL
                )
                """
            ),
            {
                "ticket_id": ticket_id,
                "repo": repo,
                "number": number,
                "title": title,
                "body": body,
                "comments": json.dumps(comments) if comments is not None else None,
                "state": state,
                "created_at": created_at,
                "updated_at": updated_at,
                "timeline": json.dumps(timeline),
            },
        )


def _pull_payload(
    *,
    number: int,
    repo: str = "pingcap/tidb",
    state: str = "closed",
    merged: bool = True,
    created_at: str = "2026-04-16T10:11:25Z",
    updated_at: str = "2026-04-23T08:05:41Z",
    closed_at: str | None = "2026-04-23T08:05:40Z",
    merged_at: str | None = "2026-04-23T08:05:40Z",
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "title": f"stabilize flaky #{number}",
        "state": state,
        "merged": merged,
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": closed_at,
        "merged_at": merged_at,
    }


def _insert_pull_ticket(
    sqlite_engine,
    *,
    ticket_id: int,
    repo: str,
    number: int,
    title: str | None = None,
    state: str = "closed",
    created_at: str = "2026-04-16T10:11:25Z",
    updated_at: str = "2026-04-23T08:05:41Z",
    closed_at: str | None = "2026-04-23T08:05:40Z",
    merged_at: str | None = "2026-04-23T08:05:40Z",
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO github_tickets (
                  id, type, repo, number, title, body, state, created_at, updated_at, closed_at, merged, merged_at, timeline, branches
                ) VALUES (
                  :ticket_id, 'pull', :repo, :number, :title, NULL, :state, :created_at, :updated_at,
                  :closed_at, :merged, :merged_at, '[]', NULL
                )
                """
            ),
            {
                "ticket_id": ticket_id,
                "repo": repo,
                "number": number,
                "title": title or f"stabilize flaky #{number}",
                "state": state,
                "created_at": created_at,
                "updated_at": updated_at,
                "closed_at": closed_at,
                "merged": merged_at is not None,
                "merged_at": merged_at,
            },
        )


def test_sync_flaky_issues_end_to_end_and_idempotent_refresh(sqlite_engine, monkeypatch) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=1,
        repo="pingcap/tidb",
        number=66726,
        title="Flaky test: TestAuditPluginRetrying in nightly",
        body="Automated flaky test report.\n\n- Branch: master\n",
        state="closed",
        created_at="2026-04-09T08:00:00Z",
        updated_at="2026-04-13T12:00:00Z",
        timeline=[
            {"event": "closed", "created_at": "2026-04-11T08:00:00Z"},
            {"event": "reopened", "created_at": "2026-04-12T08:00:00Z"},
            {"event": "closed", "created_at": "2026-04-13T09:30:00Z"},
        ],
    )
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=2,
        repo="pingcap/tidb",
        number=80000,
        title="RFC: not a flaky issue",
        state="open",
        created_at="2026-04-10T08:00:00Z",
        updated_at="2026-04-10T08:00:00Z",
        timeline=[],
    )

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("GitHub API should not be called when source ticket has branch")
        ),
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=1))

    assert summary.source_rows_scanned == 1
    assert summary.rows_written == 1
    assert summary.issue_pr_links_written == 0
    assert summary.branch_fetch_attempted == 0
    assert summary.branch_fetch_failed == 0
    assert summary.last_ticket_updated_at == "2026-04-13T12:00:00Z"

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT
                  repo,
                  issue_number,
                  case_name,
                  issue_status,
                  issue_branch,
                  branch_source,
                  issue_closed_at,
                  last_reopened_at,
                  reopen_count
                FROM ci_l1_flaky_issues
                WHERE repo = 'pingcap/tidb' AND issue_number = 66726
                """
            )
        ).mappings().one()
        state = get_job_state(connection, "ci-sync-flaky-issues")

    assert row["repo"] == "pingcap/tidb"
    assert row["issue_number"] == 66726
    assert row["case_name"] == "TestAuditPluginRetrying"
    assert row["issue_status"] == "closed"
    assert row["issue_branch"] == "master"
    assert row["branch_source"] == "default_master"
    assert str(row["issue_closed_at"]).startswith("2026-04-13 09:30:00")
    assert str(row["last_reopened_at"]).startswith("2026-04-12 08:00:00")
    assert row["reopen_count"] == 1
    assert state is not None
    assert state.last_status == "succeeded"
    assert state.watermark == {"last_ticket_updated_at": "2026-04-13T12:00:00Z"}

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE github_tickets
                SET state = 'open',
                    updated_at = '2026-04-14T09:00:00Z',
                    timeline = :timeline
                WHERE repo = 'pingcap/tidb' AND number = 66726
                """
            ),
            {
                "timeline": json.dumps(
                    [
                        {"event": "closed", "created_at": "2026-04-11T08:00:00Z"},
                        {"event": "reopened", "created_at": "2026-04-12T08:00:00Z"},
                    ]
                )
            },
        )

    second_summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=5))
    assert second_summary.source_rows_scanned == 1
    assert second_summary.rows_written == 1
    assert second_summary.issue_pr_links_written == 0
    assert second_summary.last_ticket_updated_at == "2026-04-14T09:00:00Z"

    with sqlite_engine.begin() as connection:
        refreshed_row = connection.execute(
            text(
                """
                SELECT issue_status, issue_closed_at, last_reopened_at, reopen_count
                FROM ci_l1_flaky_issues
                WHERE repo = 'pingcap/tidb' AND issue_number = 66726
                """
            )
        ).mappings().one()
        total_rows = connection.execute(
            text("SELECT COUNT(*) AS count FROM ci_l1_flaky_issues")
        ).mappings().one()

    assert refreshed_row["issue_status"] == "open"
    assert str(refreshed_row["issue_closed_at"]).startswith("2026-04-11 08:00:00")
    assert str(refreshed_row["last_reopened_at"]).startswith("2026-04-12 08:00:00")
    assert refreshed_row["reopen_count"] == 1
    assert total_rows["count"] == 1


def test_sync_flaky_issues_defaults_branch_to_master_when_ticket_body_missing(
    sqlite_engine,
    monkeypatch,
) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=3,
        repo="pingcap/tidb",
        number=70000,
        title="Flaky test: TestExample in nightly",
        body=None,
        comments=None,
        state="open",
        created_at="2026-04-10T08:00:00Z",
        updated_at="2026-04-15T09:00:00Z",
        timeline=[],
    )

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("GitHub API should not be called for branch metadata")
        ),
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=10))

    assert summary.branch_fetch_attempted == 0
    assert summary.branch_fetch_failed == 0

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT issue_branch, branch_source
                FROM ci_l1_flaky_issues
                WHERE repo = 'pingcap/tidb' AND issue_number = 70000
                """
            )
        ).mappings().one()

    assert row["issue_branch"] == "master"
    assert row["branch_source"] == "default_master"


def test_sync_flaky_issues_writes_and_replaces_issue_pr_links(
    sqlite_engine,
    monkeypatch,
) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=10,
        repo="pingcap/tidb",
        number=67740,
        title="Flaky test: TestUnregisterAfterPause in br/pkg/streamhelper",
        body="Automated flaky test report.\n\n- Branch: master\n",
        state="closed",
        created_at="2026-04-14T00:05:37Z",
        updated_at="2026-04-24T04:09:53Z",
        timeline=[
            {
                "event": "cross-referenced",
                "created_at": "2026-04-16T10:11:26Z",
                "updated_at": "2026-04-16T10:11:26Z",
                "source": {
                    "type": "issue",
                    "issue": {
                        "number": 67822,
                        "title": "br/pkg/streamhelper: stabilize flaky TestUnregisterAfterPause",
                        "repository": {"full_name": "pingcap/tidb"},
                        "pull_request": {"merged_at": "2026-04-23T08:05:40Z"},
                    },
                },
            },
            {"event": "closed", "created_at": "2026-04-23T08:05:41Z"},
        ],
    )

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("GitHub API should not be called when source ticket has branch")
        ),
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_pull_details_via_github_api",
        lambda **kwargs: _pull_payload(number=kwargs["pr_number"]),
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=10))

    assert summary.rows_written == 1
    assert summary.issue_pr_links_written == 1
    assert summary.linked_pr_rows_written == 1
    assert summary.linked_pr_fetch_attempted == 1
    assert summary.linked_pr_fetch_failed == 0

    with sqlite_engine.begin() as connection:
        links = connection.execute(
            text(
                """
                SELECT
                  issue_repo,
                  issue_number,
                  pr_repo,
                  pr_number,
                  pr_url,
                  pr_title,
                  link_type,
                  source_event_type,
                  linked_at
                FROM ci_l1_flaky_issue_pr_links
                WHERE issue_repo = 'pingcap/tidb' AND issue_number = 67740
                ORDER BY pr_number
                """
            )
        ).mappings().all()
        linked_pr = connection.execute(
            text(
                """
                SELECT
                  pr_number,
                  pr_url,
                  pr_title,
                  pr_state,
                  pr_created_at,
                  pr_closed_at,
                  pr_merged_at
                FROM ci_l1_flaky_linked_prs
                WHERE pr_repo = 'pingcap/tidb' AND pr_number = 67822
                """
            )
        ).mappings().one()

    assert len(links) == 1
    assert links[0]["pr_repo"] == "pingcap/tidb"
    assert links[0]["pr_number"] == 67822
    assert links[0]["pr_url"] == "https://github.com/pingcap/tidb/pull/67822"
    assert links[0]["link_type"] == "linked_pull_request"
    assert links[0]["source_event_type"] == "cross-referenced"
    assert str(links[0]["linked_at"]).startswith("2026-04-16 10:11:26")
    assert linked_pr["pr_number"] == 67822
    assert linked_pr["pr_url"] == "https://github.com/pingcap/tidb/pull/67822"
    assert linked_pr["pr_title"] == "stabilize flaky #67822"
    assert linked_pr["pr_state"] == "closed"
    assert str(linked_pr["pr_created_at"]).startswith("2026-04-16 10:11:25")
    assert str(linked_pr["pr_closed_at"]).startswith("2026-04-23 08:05:40")
    assert str(linked_pr["pr_merged_at"]).startswith("2026-04-23 08:05:40")

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE github_tickets
                SET updated_at = '2026-04-25T04:09:53Z',
                    timeline = :timeline
                WHERE repo = 'pingcap/tidb' AND number = 67740
                """
            ),
            {
                "timeline": json.dumps(
                    [
                        {
                            "event": "cross-referenced",
                            "created_at": "2026-04-24T09:00:00Z",
                            "updated_at": "2026-04-24T09:00:00Z",
                            "source": {
                                "type": "issue",
                                "issue": {
                                    "number": 67871,
                                    "title": "br/pkg/streamhelper: stabilize flaky TestUnregisterAfterPause v2",
                                    "repository": {"full_name": "pingcap/tidb"},
                                    "pull_request": {},
                                },
                            },
                        }
                    ]
                )
            },
        )

    second_summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=10))
    assert second_summary.issue_pr_links_written == 1
    assert second_summary.linked_pr_rows_written == 1

    with sqlite_engine.begin() as connection:
        links = connection.execute(
            text(
                """
                SELECT pr_number
                FROM ci_l1_flaky_issue_pr_links
                WHERE issue_repo = 'pingcap/tidb' AND issue_number = 67740
                ORDER BY pr_number
                """
            )
        ).mappings().all()
        linked_prs = connection.execute(
            text(
                """
                SELECT pr_number
                FROM ci_l1_flaky_linked_prs
                ORDER BY pr_number
                """
            )
        ).mappings().all()

    assert [row["pr_number"] for row in links] == [67871]
    assert [row["pr_number"] for row in linked_prs] == [67871]


def test_sync_flaky_issues_fetches_linked_pr_metadata_once_across_batches(
    sqlite_engine,
    monkeypatch,
) -> None:
    timeline = [
        {
            "event": "cross-referenced",
            "created_at": "2026-04-16T10:11:26Z",
            "source": {
                "type": "issue",
                "issue": {
                    "number": 67822,
                    "title": "br/pkg/streamhelper: stabilize flaky TestShared",
                    "repository": {"full_name": "pingcap/tidb"},
                    "pull_request": {},
                },
            },
        }
    ]
    for index, issue_number in enumerate((67740, 67741), start=1):
        _insert_issue_ticket(
            sqlite_engine,
            ticket_id=20 + index,
            repo="pingcap/tidb",
            number=issue_number,
            title=f"Flaky test: TestShared{index} in nightly",
            body="Automated flaky test report.\n\n- Branch: master\n",
            state="open",
            created_at="2026-04-14T00:05:37Z",
            updated_at=f"2026-04-24T04:09:5{index}Z",
            timeline=timeline,
        )

    fetched_pr_numbers: list[int] = []

    def _fetch_pull(**kwargs):
        fetched_pr_numbers.append(kwargs["pr_number"])
        return _pull_payload(number=kwargs["pr_number"])

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_pull_details_via_github_api",
        _fetch_pull,
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=1))

    assert summary.issue_pr_links_written == 2
    assert summary.linked_pr_rows_written == 1
    assert summary.linked_pr_fetch_attempted == 1
    assert fetched_pr_numbers == [67822]

    with sqlite_engine.begin() as connection:
        links = connection.execute(
            text(
                """
                SELECT issue_number, pr_number
                FROM ci_l1_flaky_issue_pr_links
                ORDER BY issue_number
                """
            )
        ).mappings().all()
        linked_prs = connection.execute(
            text(
                """
                SELECT pr_number
                FROM ci_l1_flaky_linked_prs
                """
            )
        ).mappings().all()

    assert [(row["issue_number"], row["pr_number"]) for row in links] == [
        (67740, 67822),
        (67741, 67822),
    ]
    assert [row["pr_number"] for row in linked_prs] == [67822]


def test_sync_flaky_issues_supports_pd_body_pr_fallback(
    sqlite_engine,
    monkeypatch,
) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=30,
        repo="tikv/pd",
        number=10858,
        title="Flaky test: TestUpgradingPDAndTSOClusters",
        body=None,
        comments=None,
        state="open",
        created_at="2026-06-09T21:31:40Z",
        updated_at="2026-06-09T21:31:40Z",
        timeline=[
            {
                "event": "labeled",
                "id": 26544302015,
                "created_at": "2026-06-09T21:31:42Z",
            }
        ],
    )
    _insert_pull_ticket(sqlite_engine, ticket_id=31, repo="tikv/pd", number=10597)
    _insert_pull_ticket(sqlite_engine, ticket_id=32, repo="tikv/pd", number=10600)
    _insert_pull_ticket(sqlite_engine, ticket_id=33, repo="tikv/pd", number=10846)

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: (
            "## Flaky Test\n"
            "### CI link\n"
            "* https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/tikv_pd/10597/pull-unit-test-next-gen-3/2064319827626954752\n"
            "### Anything else\n"
            "* PRs: 10597, 10600, 10846\n",
            [],
        ),
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_pull_details_via_github_api",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("linked PR metadata should come from github_tickets before GitHub API")
        ),
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=10))

    assert summary.source_rows_scanned == 1
    assert summary.rows_written == 1
    assert summary.issue_pr_links_written == 3
    assert summary.linked_pr_rows_written == 3
    assert summary.linked_pr_fetch_attempted == 0
    assert summary.linked_pr_fetch_failed == 0

    with sqlite_engine.begin() as connection:
        issue_row = connection.execute(
            text(
                """
                SELECT repo, issue_number, issue_status, issue_branch, branch_source
                FROM ci_l1_flaky_issues
                WHERE repo = 'tikv/pd' AND issue_number = 10858
                """
            )
        ).mappings().one()
        links = connection.execute(
            text(
                """
                SELECT pr_repo, pr_number, source_event_type
                FROM ci_l1_flaky_issue_pr_links
                WHERE issue_repo = 'tikv/pd' AND issue_number = 10858
                ORDER BY pr_number
                """
            )
        ).mappings().all()
        linked_prs = connection.execute(
            text(
                """
                SELECT pr_repo, pr_number
                FROM ci_l1_flaky_linked_prs
                ORDER BY pr_number
                """
            )
        ).mappings().all()

    assert issue_row["issue_status"] == "open"
    assert issue_row["issue_branch"] == "master"
    assert issue_row["branch_source"] == "default_master"
    assert [(row["pr_repo"], row["pr_number"], row["source_event_type"]) for row in links] == [
        ("tikv/pd", 10597, "issue_body_pr_list"),
        ("tikv/pd", 10600, "issue_body_pr_list"),
        ("tikv/pd", 10846, "issue_body_pr_list"),
    ]
    assert [(row["pr_repo"], row["pr_number"]) for row in linked_prs] == [
        ("tikv/pd", 10597),
        ("tikv/pd", 10600),
        ("tikv/pd", 10846),
    ]


def test_sync_flaky_issues_merges_timeline_and_fetched_body_pr_links_for_pd(
    sqlite_engine,
    monkeypatch,
) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=34,
        repo="tikv/pd",
        number=10859,
        title="Flaky test: TestUpgradingPDAndTSOClusters",
        body=None,
        comments=None,
        state="open",
        created_at="2026-06-09T21:31:40Z",
        updated_at="2026-06-09T21:31:40Z",
        timeline=[
            {
                "event": "cross-referenced",
                "id": 26544302016,
                "created_at": "2026-06-09T21:31:42Z",
                "source": {
                    "type": "issue",
                    "issue": {
                        "number": 10597,
                        "title": "stabilize flaky",
                        "repository": {"full_name": "tikv/pd"},
                        "pull_request": {},
                    },
                },
            }
        ],
    )
    _insert_pull_ticket(sqlite_engine, ticket_id=35, repo="tikv/pd", number=10597)
    _insert_pull_ticket(sqlite_engine, ticket_id=36, repo="tikv/pd", number=10600)

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: ("PRs: 10597, 10600", []),
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_pull_details_via_github_api",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("linked PR metadata should come from github_tickets before GitHub API")
        ),
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=10))

    assert summary.source_rows_scanned == 1
    assert summary.issue_pr_links_written == 2

    with sqlite_engine.begin() as connection:
        links = connection.execute(
            text(
                """
                SELECT pr_repo, pr_number, source_event_type
                FROM ci_l1_flaky_issue_pr_links
                WHERE issue_repo = 'tikv/pd' AND issue_number = 10859
                ORDER BY pr_number
                """
            )
        ).mappings().all()

    assert [(row["pr_repo"], row["pr_number"], row["source_event_type"]) for row in links] == [
        ("tikv/pd", 10597, "cross-referenced"),
        ("tikv/pd", 10600, "issue_body_pr_list"),
    ]


def test_backfill_flaky_issue_pr_links_rebuilds_links_without_touching_issue_rows(
    sqlite_engine,
    monkeypatch,
) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=11,
        repo="pingcap/tidb",
        number=67563,
        title="Flaky test: TestBatchCoprocessor in tidb",
        body="Automated flaky test report.\n\n- Branch: release-8.5\n",
        state="closed",
        created_at="2026-04-10T00:00:00Z",
        updated_at="2026-04-26T09:15:00Z",
        timeline=[
            {
                "id": 3001,
                "event": "cross-referenced",
                "created_at": "2026-04-20T10:00:00Z",
                "source": {
                    "type": "issue",
                    "issue": {
                        "number": 67601,
                        "title": "executor: stabilize flaky TestBatchCoprocessor",
                        "repository": {"full_name": "pingcap/tidb"},
                        "pull_request": {},
                    },
                },
            },
            {
                "id": 3002,
                "event": "cross-referenced",
                "created_at": "2026-04-21T10:00:00Z",
                "source": {
                    "type": "issue",
                    "issue": {
                        "number": 67644,
                        "title": "executor: stabilize flaky TestBatchCoprocessor follow-up",
                        "repository": {"full_name": "pingcap/tidb"},
                        "pull_request": {},
                    },
                },
            },
        ],
    )

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_flaky_issues (
                  repo,
                  issue_number,
                  issue_url,
                  issue_title,
                  case_name,
                  issue_status,
                  issue_branch,
                  branch_source,
                  issue_created_at,
                  issue_updated_at,
                  issue_closed_at,
                  last_reopened_at,
                  reopen_count,
                  source_ticket_id,
                  source_ticket_updated_at,
                  created_at,
                  updated_at
                ) VALUES (
                  'pingcap/tidb',
                  67563,
                  'https://github.com/pingcap/tidb/issues/67563',
                  'Flaky test: TestBatchCoprocessor in tidb',
                  'TestBatchCoprocessor',
                  'closed',
                  'release-8.4',
                  'manual_seed',
                  '2026-04-10 00:00:00',
                  '2026-04-26 09:15:00',
                  NULL,
                  NULL,
                  0,
                  11,
                  '2026-04-26 09:15:00',
                  CURRENT_TIMESTAMP,
                  CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_flaky_issue_pr_links (
                  issue_repo,
                  issue_number,
                  pr_repo,
                  pr_number,
                  pr_url,
                  pr_title,
                  link_type,
                  source_event_type,
                  source_event_id,
                  linked_at,
                  source_ticket_updated_at
                ) VALUES (
                  'pingcap/tidb',
                  67563,
                  'pingcap/tidb',
                  67599,
                  'https://github.com/pingcap/tidb/pull/67599',
                  'stale link',
                  'linked_pull_request',
                  'cross-referenced',
                  2999,
                  '2026-04-19 10:00:00',
                  '2026-04-25 09:15:00'
                )
                """
            )
        )

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_pull_details_via_github_api",
        lambda **kwargs: _pull_payload(number=kwargs["pr_number"]),
    )

    summary = run_backfill_flaky_issue_pr_links(sqlite_engine, _settings(batch_size=1))

    assert summary.batches_processed == 1
    assert summary.source_rows_scanned == 1
    assert summary.issue_rows_touched == 1
    assert summary.issue_pr_links_written == 2
    assert summary.linked_pr_rows_written == 2
    assert summary.linked_pr_fetch_attempted == 2
    assert summary.linked_pr_fetch_failed == 0
    assert summary.last_ticket_updated_at == "2026-04-26T09:15:00Z"

    with sqlite_engine.begin() as connection:
        issue_row = connection.execute(
            text(
                """
                SELECT issue_branch, branch_source
                FROM ci_l1_flaky_issues
                WHERE repo = 'pingcap/tidb' AND issue_number = 67563
                """
            )
        ).mappings().one()
        links = connection.execute(
            text(
                """
                SELECT pr_number
                FROM ci_l1_flaky_issue_pr_links
                WHERE issue_repo = 'pingcap/tidb' AND issue_number = 67563
                ORDER BY pr_number
                """
            )
        ).mappings().all()
        linked_prs = connection.execute(
            text(
                """
                SELECT pr_number
                FROM ci_l1_flaky_linked_prs
                ORDER BY pr_number
                """
            )
        ).mappings().all()

    assert issue_row["issue_branch"] == "release-8.4"
    assert issue_row["branch_source"] == "manual_seed"
    assert [row["pr_number"] for row in links] == [67601, 67644]
    assert [row["pr_number"] for row in linked_prs] == [67601, 67644]


def test_sync_flaky_issues_reuses_existing_branch_when_ticket_is_unchanged(
    sqlite_engine,
    monkeypatch,
) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=4,
        repo="pingcap/tidb",
        number=70001,
        title="Flaky test: TestExampleStable in nightly",
        body=None,
        comments=None,
        state="open",
        created_at="2026-04-10T08:00:00Z",
        updated_at="2026-04-15T09:00:00Z",
        timeline=[],
    )

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_flaky_issues (
                  repo,
                  issue_number,
                  issue_url,
                  issue_title,
                  case_name,
                  issue_status,
                  issue_branch,
                  branch_source,
                  issue_created_at,
                  issue_updated_at,
                  issue_closed_at,
                  last_reopened_at,
                  reopen_count,
                  source_ticket_id,
                  source_ticket_updated_at,
                  created_at,
                  updated_at
                ) VALUES (
                  'pingcap/tidb',
                  70001,
                  'https://github.com/pingcap/tidb/issues/70001',
                  'Flaky test: TestExampleStable in nightly',
                  'TestExampleStable',
                  'open',
                  'master',
                  'github_api_body',
                  '2026-04-10 08:00:00',
                  '2026-04-15 09:00:00',
                  NULL,
                  NULL,
                  0,
                  4,
                  '2026-04-15 09:00:00',
                  CURRENT_TIMESTAMP,
                  CURRENT_TIMESTAMP
                )
                """
            )
        )

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("GitHub API should not be called for unchanged issue rows")
        ),
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=10))

    assert summary.branch_fetch_attempted == 0
    assert summary.branch_fetch_failed == 0

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT issue_branch, branch_source
                FROM ci_l1_flaky_issues
                WHERE repo = 'pingcap/tidb' AND issue_number = 70001
                """
            )
        ).mappings().one()

    assert row["issue_branch"] == "master"
    assert row["branch_source"] == "default_master"


def test_parse_issue_branch_handles_plain_body_and_escaped_newlines() -> None:
    issue_body = '{"body":"Automated flaky test report.\\n- Branch: release-8.5\\n- Other: value"}'
    assert parse_issue_branch(issue_body) == "release-8.5"


def test_parse_issue_branch_from_comments_prefers_latest_bot_comment() -> None:
    comments = [
        {"author": "someone", "body": "No branch here"},
        {"author": "ti-chi-bot", "body": "Automated flaky test report update.\n- Branch: master\n"},
        {"author": "ti-chi-bot", "body": "Automated flaky test report update.\n- Branch: release-8.5\n"},
    ]
    assert parse_issue_branch_from_comments(comments) == "release-8.5"


def test_sync_flaky_issue_helpers_cover_fallbacks_and_payload_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_issue_branch_from_comments(None) is None
    assert parse_issue_branch_from_comments("not-json") is None
    assert parse_issue_branch_from_comments([{"user": {"login": "someone"}, "body": "- Branch: release-9.0"}]) == "release-9.0"

    assert _normalize_issue_comments(None) == []
    assert _normalize_issue_comments('[{"body":"ok"},{"bad":1},3]') == [{"body": "ok"}, {"bad": 1}]
    with pytest.raises(ValueError, match="Unsupported issue comments payload"):
        _normalize_issue_comments(123)

    assert _parse_case_name("Flaky test: TestCase in nightly") == "TestCase"
    assert _parse_case_name("Flaky test: TestCase") == "TestCase"
    assert _parse_case_name("Plain title") == "Plain title"
    assert _parse_timeline(None) == []
    assert _parse_timeline('[{"event":"closed"},"bad"]') == [{"event": "closed"}]
    with pytest.raises(ValueError, match="Unsupported issue timeline payload"):
        _parse_timeline(123)

    closed_at, reopened_at, reopen_count = _extract_issue_lifecycle(
        [
            {"event": "closed", "created_at": "2026-04-11T08:00:00Z"},
            {"event": "reopened", "createdAt": "2026-04-12T08:00:00Z"},
            {"event": "ignored"},
        ]
    )
    assert str(closed_at).startswith("2026-04-11 08:00:00")
    assert str(reopened_at).startswith("2026-04-12 08:00:00")
    assert reopen_count == 1

    now = datetime(2026, 4, 15, 9, 0, 0)
    assert _parse_datetime(now) == now
    assert _parse_datetime("2026-04-15T09:00:00Z") == datetime(2026, 4, 15, 9, 0, 0)
    with pytest.raises(ValueError, match="Unsupported datetime value"):
        _parse_datetime(123)

    row = _build_flaky_issue_row(
        {
            "id": 1,
            "repo": "pingcap/tidb",
            "number": 70002,
            "title": "Flaky test: TestBranchFallback in nightly",
            "state": "open",
            "created_at": "2026-04-10T08:00:00Z",
            "updated_at": "2026-04-15T09:00:00Z",
            "timeline": [],
        },
        issue_branch="master",
        branch_source="ticket_body",
    )
    assert row.case_name == "TestBranchFallback"
    assert row.issue_branch == "master"
    assert row.branch_source == "ticket_body"

    link_rows = _extract_linked_pr_rows(
        {
            "repo": "pingcap/tidb",
            "number": 70002,
            "timeline": [
                {
                    "event": "cross-referenced",
                    "created_at": "2026-04-16T10:11:26Z",
                    "source": {
                        "type": "issue",
                        "issue": {
                            "number": 67822,
                            "title": "stabilize flaky TestBranchFallback",
                            "repository": {"full_name": "pingcap/tidb"},
                            "pull_request": {"merged_at": "2026-04-23T08:05:40Z"},
                        },
                    },
                },
                {
                    "event": "cross-referenced",
                    "created_at": "2026-04-17T10:11:26Z",
                    "source": {
                        "type": "issue",
                        "issue": {
                            "number": 67822,
                            "title": "stabilize flaky TestBranchFallback",
                            "repository": {"full_name": "pingcap/tidb"},
                            "pull_request": {"merged_at": "2026-04-23T08:05:40Z"},
                        },
                    },
                },
                {
                    "event": "cross-referenced",
                    "created_at": "2026-04-18T10:11:26Z",
                    "source": {
                        "type": "issue",
                        "issue": {
                            "number": 67871,
                            "title": "stabilize flaky TestBranchFallback again",
                            "repository": {"full_name": "pingcap/tidb"},
                            "pull_request": {},
                        },
                    },
                },
                {
                    "event": "cross-referenced",
                    "created_at": "2026-04-19T10:11:26Z",
                    "source": {
                        "type": "issue",
                        "issue": {
                            "number": 70009,
                            "title": "plain issue, not PR",
                            "repository": {"full_name": "pingcap/tidb"},
                        },
                    },
                },
            ],
        },
        source_ticket_updated_at=datetime.fromisoformat("2026-04-15T09:00:00+00:00"),
    )
    assert [(item.pr_repo, item.pr_number) for item in link_rows] == [
        ("pingcap/tidb", 67822),
        ("pingcap/tidb", 67871),
    ]
    assert str(link_rows[0].linked_at).startswith("2026-04-16 10:11:26")

    assert _parse_linked_pr_candidates_from_body(
        "PRs: 10597, 10600\n"
        "https://github.com/tikv/pd/pull/10846\n"
        "https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/tikv_pd/10597/pull-unit-test-next-gen-3/2064319827626954752\n",
        issue_repo="tikv/pd",
    ) == [
        ("tikv/pd", 10597, "issue_body_pr_list"),
        ("tikv/pd", 10600, "issue_body_pr_list"),
        ("tikv/pd", 10846, "issue_body_github_pr_url"),
    ]
    assert _parse_linked_pr_candidates_from_body(
        "https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/67822/pull-unit-test-next-gen/1\n",
        issue_repo="tikv/pd",
    ) == [
        ("pingcap/tidb", 67822, "issue_body_ci_link"),
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: ("PRs: 10597, 10600", []),
    )
    assert _enrich_issue_row_for_pr_fallback(
        {
            "repo": "tikv/pd",
            "number": 10858,
            "body": None,
            "updated_at": "2026-06-09T21:31:40Z",
            "timeline": [{"event": "labeled"}],
        }
    )["body"] == "PRs: 10597, 10600"
    assert _enrich_issue_row_for_pr_fallback(
        {
            "repo": "tikv/pd",
            "number": 10858,
            "body": None,
            "updated_at": "2026-06-09T21:31:40Z",
            "timeline": [
                {
                    "event": "cross-referenced",
                    "source": {
                        "type": "issue",
                        "issue": {
                            "number": 10597,
                            "repository": {"full_name": "tikv/pd"},
                            "pull_request": {},
                        },
                    },
                }
            ],
        }
    )["body"] == "PRs: 10597, 10600"

    existing = {
        "issue_branch": "release-8.5",
        "branch_source": "github_api_body",
        "source_ticket_updated_at": "2026-04-15T09:00:00Z",
    }
    assert _reuse_existing_issue_branch_if_fresh(
        {"updated_at": "2026-04-15T09:00:00Z"},
        existing,
    ) == ("release-8.5", "github_api_body")
    assert _reuse_existing_issue_branch_if_fresh(
        {"updated_at": "2026-04-16T09:00:00Z"},
        existing,
    ) == (None, "")
    assert _fallback_issue_branch(existing) == ("release-8.5", "github_api_body")
    assert _fallback_issue_branch(None) == (None, "unknown")

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_details_via_github_api",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("GitHub API should not be called for branch metadata")
        ),
    )
    assert _resolve_issue_branch(
        {"repo": "pingcap/tidb", "number": 1, "body": None, "comments": None, "updated_at": "2026-04-16T09:00:00Z"},
        existing,
    ) == ("master", "default_master", False, False)


def test_fetch_github_api_json_and_issue_details_wrap_http_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def __init__(self, payload: str) -> None:
            self._payload = payload.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return self._payload

    responses = iter(
        [
            _Response(
                json.dumps(
                    {
                        "body": "hello",
                        "comments_url": "https://api.github.com/comments/1",
                        "comments": 1,
                    }
                )
            ),
            _Response(json.dumps([{"body": "- Branch: master"}])),
        ]
    )
    monkeypatch.setattr("ci_dashboard.jobs.sync_flaky_issues.urllib_request.urlopen", lambda request, timeout=0: next(responses))

    body, comments = fetch_issue_details_via_github_api(repo="pingcap/tidb", issue_number=1)

    assert body == "hello"
    assert comments == [{"body": "- Branch: master"}]

    class _HTTPError(urllib_error.HTTPError):
        def __init__(self) -> None:
            super().__init__("https://api.github.com", 403, "forbidden", hdrs=None, fp=None)

        def read(self) -> bytes:
            return b'{"message":"forbidden"}'

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.urllib_request.urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(_HTTPError()),
    )
    with pytest.raises(RuntimeError, match="HTTP 403"):
        _fetch_github_api_json("https://api.github.com/repos/pingcap/tidb/issues/1")

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.urllib_request.urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(urllib_error.URLError("timeout")),
    )
    with pytest.raises(RuntimeError, match="timeout"):
        _fetch_github_api_json("https://api.github.com/repos/pingcap/tidb/issues/1")


def test_fetch_github_api_json_retries_rate_limited_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def __init__(self, payload: str) -> None:
            self._payload = payload.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return self._payload

    class _HTTPError(urllib_error.HTTPError):
        def __init__(self) -> None:
            super().__init__(
                "https://api.github.com",
                429,
                "rate limited",
                hdrs={"Retry-After": "0"},
                fp=None,
            )

        def read(self) -> bytes:
            return b'{"message":"rate limit exceeded"}'

    responses = iter(
        [
            _HTTPError(),
            _Response(json.dumps({"ok": True})),
        ]
    )
    sleep_calls: list[float] = []

    def _fake_urlopen(request, timeout=0):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues._last_github_api_request_monotonic",
        None,
    )
    monkeypatch.setattr("ci_dashboard.jobs.sync_flaky_issues.urllib_request.urlopen", _fake_urlopen)
    monkeypatch.setattr("ci_dashboard.jobs.sync_flaky_issues.time.sleep", sleep_calls.append)
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.time.monotonic",
        iter([0.0, 0.0, 1.0, 1.0]).__next__,
    )

    assert _fetch_github_api_json("https://api.github.com/repos/pingcap/tidb/issues/1") == {"ok": True}
    assert sleep_calls == [0.0]


def test_upsert_flaky_issues_accepts_empty_rows_and_tidb_statement(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        _upsert_flaky_issues(connection, [])

    tidb_connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    statement = _build_upsert_statement(tidb_connection)
    assert "ON DUPLICATE KEY UPDATE" in str(statement)
    issue_pr_links_statement = _build_issue_pr_links_upsert_statement(tidb_connection)
    assert "ON DUPLICATE KEY UPDATE" in str(issue_pr_links_statement)
