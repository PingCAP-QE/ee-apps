from __future__ import annotations

import json

from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.state_store import get_job_state
from ci_dashboard.jobs.sync_flaky_issues import parse_issue_branch, run_sync_flaky_issues


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
                  id, type, repo, number, title, body, state, created_at, updated_at, timeline, branches
                ) VALUES (
                  :ticket_id, 'issue', :repo, :number, :title, :body, :state, :created_at, :updated_at,
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
                "state": state,
                "created_at": created_at,
                "updated_at": updated_at,
                "timeline": json.dumps(timeline),
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
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_body_via_gh",
        lambda **_: (_ for _ in ()).throw(AssertionError("gh should not be called when ticket body has branch")),
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=1))

    assert summary.source_rows_scanned == 1
    assert summary.rows_written == 1
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
    assert row["branch_source"] == "ticket_body"
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


def test_sync_flaky_issues_uses_gh_body_when_ticket_body_missing(sqlite_engine, monkeypatch) -> None:
    _insert_issue_ticket(
        sqlite_engine,
        ticket_id=3,
        repo="pingcap/tidb",
        number=70000,
        title="Flaky test: TestExample in nightly",
        body=None,
        state="open",
        created_at="2026-04-10T08:00:00Z",
        updated_at="2026-04-15T09:00:00Z",
        timeline=[],
    )

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_flaky_issues.fetch_issue_body_via_gh",
        lambda **_: "Automated flaky test report.\n- Branch: release-8.5\n",
    )

    summary = run_sync_flaky_issues(sqlite_engine, _settings(batch_size=10))

    assert summary.branch_fetch_attempted == 1
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

    assert row["issue_branch"] == "release-8.5"
    assert row["branch_source"] == "gh_cli_body"


def test_parse_issue_branch_handles_plain_body_and_escaped_newlines() -> None:
    issue_body = '{"body":"Automated flaky test report.\\n- Branch: release-8.5\\n- Other: value"}'
    assert parse_issue_branch(issue_body) == "release-8.5"
