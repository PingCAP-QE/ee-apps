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
    _build_upsert_statement,
    _extract_issue_lifecycle,
    _fallback_issue_branch,
    _fetch_github_api_json,
    _normalize_issue_comments,
    _parse_case_name,
    _parse_datetime,
    _parse_timeline,
    _resolve_issue_branch,
    _reuse_existing_issue_branch_if_fresh,
    _upsert_flaky_issues,
    fetch_issue_details_via_github_api,
    parse_issue_branch,
    parse_issue_branch_from_comments,
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


def test_sync_flaky_issues_uses_github_api_comments_when_ticket_body_missing(
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
        lambda **_: (
            "Automated flaky test report.\n- Branch: master\n",
            [
                {
                    "user": {"login": "ti-chi-bot"},
                    "body": "Automated flaky test report update.\n- Branch: release-8.5\n",
                }
            ],
        ),
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
    assert row["branch_source"] == "github_api_comments"


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
    assert row["branch_source"] == "github_api_body"


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
    assert _parse_datetime("2026-04-15T09:00:00Z") == datetime.fromisoformat("2026-04-15T09:00:00+00:00")
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
        lambda **_: ("body without branch", []),
    )
    assert _resolve_issue_branch(
        {"repo": "pingcap/tidb", "number": 1, "body": None, "comments": None, "updated_at": "2026-04-16T09:00:00Z"},
        existing,
    ) == ("release-8.5", "github_api_body", True, False)


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


def test_upsert_flaky_issues_accepts_empty_rows_and_tidb_statement(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        _upsert_flaky_issues(connection, [])

    tidb_connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    statement = _build_upsert_statement(tidb_connection)
    assert "ON DUPLICATE KEY UPDATE" in str(statement)
