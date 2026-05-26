from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import (
    BackfillFlakyIssuePrLinksSummary,
    SyncFlakyIssuesSummary,
)
from ci_dashboard.common.sql_helpers import chunked
from ci_dashboard.jobs.state_store import (
    get_job_state,
    mark_job_failed,
    mark_job_started,
    mark_job_succeeded,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "ci-sync-flaky-issues"
DEFAULT_FLAKY_ISSUE_REPOS = ("pingcap/tidb",)
TITLE_PATTERN = "Flaky test:%"
CASE_NAME_PATTERN = re.compile(r"^Flaky test:\s*(.+?)\s+in\s+.+$")
BRANCH_PATTERN = re.compile(r"- Branch:\s*([^\n<\"]+)")
GITHUB_API_BASE_URL = "https://api.github.com"
BOT_AUTHORS = {"ti-chi-bot", "ti-chi-bot[bot]"}


@dataclass(frozen=True)
class FlakyIssueRow:
    repo: str
    issue_number: int
    issue_url: str
    issue_title: str
    case_name: str
    issue_status: str
    issue_branch: str | None
    branch_source: str
    issue_created_at: datetime
    issue_updated_at: datetime
    issue_closed_at: datetime | None
    last_reopened_at: datetime | None
    reopen_count: int
    source_ticket_id: int
    source_ticket_updated_at: datetime


@dataclass(frozen=True)
class FlakyIssuePrLinkRow:
    issue_repo: str
    issue_number: int
    pr_repo: str
    pr_number: int
    pr_url: str
    pr_title: str
    link_type: str
    source_event_type: str
    source_event_id: int | None
    linked_at: datetime
    source_ticket_updated_at: datetime


def run_sync_flaky_issues(engine: Engine, settings: Settings) -> SyncFlakyIssuesSummary:
    with engine.begin() as connection:
        watermark = _load_watermark(connection)
        mark_job_started(connection, JOB_NAME, watermark)

    summary = SyncFlakyIssuesSummary()

    try:
        with engine.begin() as connection:
            source_rows = _fetch_source_issue_rows(connection, DEFAULT_FLAKY_ISSUE_REPOS)
            existing_rows = _load_existing_issue_rows(connection)

        summary.source_rows_scanned = len(source_rows)

        prepared_batches: list[tuple[FlakyIssueRow, list[FlakyIssuePrLinkRow]]] = []
        latest_updated_at: datetime | None = None

        for row in source_rows:
            source_updated_at = _parse_datetime(row["updated_at"])
            if latest_updated_at is None or source_updated_at > latest_updated_at:
                latest_updated_at = source_updated_at

            existing = existing_rows.get((str(row["repo"]), int(row["number"])))
            issue_branch, branch_source, used_gh, gh_failed = _resolve_issue_branch(row, existing)
            if used_gh:
                summary.branch_fetch_attempted += 1
            if gh_failed:
                summary.branch_fetch_failed += 1

            issue_row = _build_flaky_issue_row(
                row,
                issue_branch=issue_branch,
                branch_source=branch_source,
            )
            link_rows = _extract_linked_pr_rows(
                row,
                source_ticket_updated_at=issue_row.source_ticket_updated_at,
            )
            prepared_batches.append((issue_row, link_rows))

        for batch in chunked(prepared_batches, settings.jobs.batch_size):
            with engine.begin() as connection:
                issue_rows = [item[0] for item in batch]
                link_rows = [link_row for _issue_row, links in batch for link_row in links]
                issue_keys = [(issue_row.repo, issue_row.issue_number) for issue_row in issue_rows]

                _upsert_flaky_issues(connection, issue_rows)
                _replace_flaky_issue_pr_links(
                    connection,
                    issue_keys=issue_keys,
                    rows=link_rows,
                )
                summary.rows_written += len(issue_rows)
                summary.issue_pr_links_written += len(link_rows)

        new_watermark = {
            "last_ticket_updated_at": latest_updated_at.isoformat().replace("+00:00", "Z")
            if latest_updated_at
            else None
        }
        with engine.begin() as connection:
            mark_job_succeeded(connection, JOB_NAME, new_watermark)

        summary.last_ticket_updated_at = new_watermark["last_ticket_updated_at"]
        return summary
    except Exception as exc:
        with engine.begin() as connection:
            mark_job_failed(connection, JOB_NAME, watermark, str(exc))
        raise


def run_backfill_flaky_issue_pr_links(
    engine: Engine,
    settings: Settings,
) -> BackfillFlakyIssuePrLinksSummary:
    summary = BackfillFlakyIssuePrLinksSummary()

    with engine.begin() as connection:
        source_rows = _fetch_source_issue_rows(connection, DEFAULT_FLAKY_ISSUE_REPOS)

    summary.source_rows_scanned = len(source_rows)
    latest_updated_at: datetime | None = None

    for batch in chunked(source_rows, settings.jobs.batch_size):
        issue_keys: list[tuple[str, int]] = []
        link_rows: list[FlakyIssuePrLinkRow] = []

        for row in batch:
            source_updated_at = _parse_datetime(row["updated_at"])
            if latest_updated_at is None or source_updated_at > latest_updated_at:
                latest_updated_at = source_updated_at

            issue_keys.append((str(row["repo"]), int(row["number"])))
            link_rows.extend(
                _extract_linked_pr_rows(
                    row,
                    source_ticket_updated_at=source_updated_at,
                )
            )

        with engine.begin() as connection:
            _replace_flaky_issue_pr_links(
                connection,
                issue_keys=issue_keys,
                rows=link_rows,
            )

        summary.batches_processed += 1
        summary.issue_rows_touched += len(issue_keys)
        summary.issue_pr_links_written += len(link_rows)

    summary.last_ticket_updated_at = (
        latest_updated_at.isoformat().replace("+00:00", "Z")
        if latest_updated_at
        else None
    )
    return summary


def fetch_issue_details_via_github_api(
    *,
    repo: str,
    issue_number: int,
) -> tuple[str, list[dict[str, Any]]]:
    issue_payload = _fetch_github_api_json(
        f"{GITHUB_API_BASE_URL}/repos/{repo}/issues/{issue_number}"
    )
    if not isinstance(issue_payload, dict):
        raise RuntimeError("GitHub issue payload is not an object")

    body = str(issue_payload.get("body") or "")
    comments: list[dict[str, Any]] = []
    comments_url = issue_payload.get("comments_url")
    comments_count = issue_payload.get("comments")
    if isinstance(comments_url, str) and comments_url and int(comments_count or 0) > 0:
        comments_payload = _fetch_github_api_json(comments_url)
        if isinstance(comments_payload, list):
            comments = [item for item in comments_payload if isinstance(item, dict)]
    return body, comments


def _fetch_github_api_json(url: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ci-dashboard-sync-flaky-issues",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib_request.Request(url, headers=headers)
    try:
        with urllib_request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API request failed for {url}: HTTP {exc.code}: {body}"
        ) from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"GitHub API request failed for {url}: {exc.reason}") from exc


def parse_issue_branch(issue_body: str) -> str | None:
    normalized = html.unescape(issue_body).replace("\r\n", "\n").replace("\\n", "\n")
    match = BRANCH_PATTERN.search(normalized)
    if match is None:
        return None
    return match.group(1).strip() or None


def parse_issue_branch_from_comments(comments_payload: Any) -> str | None:
    try:
        comments = _normalize_issue_comments(comments_payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not comments:
        return None

    for preferred_authors in (BOT_AUTHORS, None):
        for comment in reversed(comments):
            author = _extract_comment_author(comment)
            if preferred_authors is not None and author not in preferred_authors:
                continue
            branch = parse_issue_branch(_extract_comment_body(comment))
            if branch:
                return branch
    return None


def _resolve_issue_branch(
    row: dict[str, Any],
    existing: dict[str, Any] | None,
) -> tuple[str | None, str, bool, bool]:
    source_comments_branch = parse_issue_branch_from_comments(row.get("comments"))
    if source_comments_branch:
        return source_comments_branch, "ticket_comments", False, False

    ticket_body = str(row.get("body") or "")
    body_branch = parse_issue_branch(ticket_body)
    if body_branch:
        return body_branch, "ticket_body", False, False

    reused_branch, reused_source = _reuse_existing_issue_branch_if_fresh(row, existing)
    if reused_branch:
        return reused_branch, reused_source, False, False

    repo = str(row["repo"])
    issue_number = int(row["number"])
    try:
        github_body, github_comments = fetch_issue_details_via_github_api(
            repo=repo,
            issue_number=issue_number,
        )
        github_comments_branch = parse_issue_branch_from_comments(github_comments)
        if github_comments_branch:
            return github_comments_branch, "github_api_comments", True, False
        github_body_branch = parse_issue_branch(github_body)
        if github_body_branch:
            return github_body_branch, "github_api_body", True, False
        fallback_branch, fallback_source = _fallback_issue_branch(existing)
        return fallback_branch, fallback_source, True, False
    except Exception as exc:
        LOG.warning(
            "failed to fetch flaky issue details via GitHub API",
            extra={
                "repo": repo,
                "issue_number": issue_number,
                "error": str(exc),
            },
        )
        fallback_branch, fallback_source = _fallback_issue_branch(existing)
        return fallback_branch, fallback_source, True, True


def _fallback_issue_branch(existing: dict[str, Any] | None) -> tuple[str | None, str]:
    if existing and existing.get("issue_branch"):
        return existing["issue_branch"], str(existing.get("branch_source") or "existing")
    return None, "unknown"


def _reuse_existing_issue_branch_if_fresh(
    row: dict[str, Any],
    existing: dict[str, Any] | None,
) -> tuple[str | None, str]:
    if not existing or not existing.get("issue_branch"):
        return None, ""

    source_updated_at = _parse_datetime(row["updated_at"])
    existing_updated_at_raw = existing.get("source_ticket_updated_at")
    if existing_updated_at_raw is None:
        return None, ""
    existing_updated_at = _parse_datetime(existing_updated_at_raw)
    if _normalize_datetime_key(existing_updated_at) != _normalize_datetime_key(source_updated_at):
        return None, ""
    return str(existing["issue_branch"]), str(existing.get("branch_source") or "existing")


def _normalize_issue_comments(comments_payload: Any) -> list[dict[str, Any]]:
    if comments_payload is None:
        return []
    if isinstance(comments_payload, list):
        return [item for item in comments_payload if isinstance(item, dict)]
    if isinstance(comments_payload, str):
        parsed = json.loads(comments_payload)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    raise ValueError(f"Unsupported issue comments payload: {comments_payload!r}")


def _extract_comment_author(comment: dict[str, Any]) -> str:
    author = comment.get("author")
    if isinstance(author, str):
        return author
    user = comment.get("user")
    if isinstance(user, dict):
        login = user.get("login")
        if isinstance(login, str):
            return login
    return ""


def _extract_comment_body(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    return str(body) if body is not None else ""


def _load_watermark(connection: Connection) -> dict[str, Any]:
    state = get_job_state(connection, JOB_NAME)
    if state is None:
        return {"last_ticket_updated_at": None}
    return {
        "last_ticket_updated_at": state.watermark.get("last_ticket_updated_at"),
    }


def _fetch_source_issue_rows(
    connection: Connection,
    repos: tuple[str, ...],
) -> list[dict[str, Any]]:
    repo_params = {f"repo_{index}": repo for index, repo in enumerate(repos)}
    repo_placeholders = ", ".join(f":repo_{index}" for index in range(len(repos)))
    rows = connection.execute(
        text(
            f"""
            SELECT
              id,
              repo,
              number,
              title,
              body,
              comments,
              state,
              created_at,
              updated_at,
              timeline
            FROM github_tickets
            WHERE type = 'issue'
              AND repo IN ({repo_placeholders})
              AND title LIKE :title_pattern
            ORDER BY repo, number
            """
        ),
        {**repo_params, "title_pattern": TITLE_PATTERN},
    ).mappings()
    return [dict(row) for row in rows]


def _load_existing_issue_rows(
    connection: Connection,
) -> dict[tuple[str, int], dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT repo, issue_number, issue_branch, branch_source, source_ticket_updated_at
            FROM ci_l1_flaky_issues
            """
        )
    ).mappings()
    return {
        (str(row["repo"]), int(row["issue_number"])): dict(row)
        for row in rows
    }


def _build_flaky_issue_row(
    row: dict[str, Any],
    *,
    issue_branch: str | None,
    branch_source: str,
) -> FlakyIssueRow:
    repo = str(row["repo"])
    issue_number = int(row["number"])
    title = str(row["title"])
    timeline = _parse_timeline(row.get("timeline"))
    issue_closed_at, last_reopened_at, reopen_count = _extract_issue_lifecycle(timeline)

    return FlakyIssueRow(
        repo=repo,
        issue_number=issue_number,
        issue_url=f"https://github.com/{repo}/issues/{issue_number}",
        issue_title=title,
        case_name=_parse_case_name(title),
        issue_status=str(row["state"]),
        issue_branch=issue_branch,
        branch_source=branch_source,
        issue_created_at=_parse_datetime(row["created_at"]),
        issue_updated_at=_parse_datetime(row["updated_at"]),
        issue_closed_at=issue_closed_at,
        last_reopened_at=last_reopened_at,
        reopen_count=reopen_count,
        source_ticket_id=int(row["id"]),
        source_ticket_updated_at=_parse_datetime(row["updated_at"]),
    )


def _parse_case_name(title: str) -> str:
    match = CASE_NAME_PATTERN.match(title.strip())
    if match is not None:
        return match.group(1).strip()
    if ":" in title:
        return title.split(":", 1)[1].strip()
    return title.strip()


def _extract_linked_pr_rows(
    row: dict[str, Any],
    *,
    source_ticket_updated_at: datetime,
) -> list[FlakyIssuePrLinkRow]:
    timeline = _parse_timeline(row.get("timeline"))
    issue_repo = str(row["repo"])
    issue_number = int(row["number"])
    links_by_pr: dict[tuple[str, int], FlakyIssuePrLinkRow] = {}

    for event in timeline:
        if str(event.get("event") or "") != "cross-referenced":
            continue

        source = event.get("source")
        if not isinstance(source, dict):
            continue

        source_issue = source.get("issue")
        if not isinstance(source_issue, dict):
            continue

        pull_request_meta = source_issue.get("pull_request")
        if not isinstance(pull_request_meta, dict):
            continue

        pr_number = source_issue.get("number")
        if pr_number is None:
            continue

        try:
            normalized_pr_number = int(pr_number)
        except (TypeError, ValueError):
            continue

        repository = source_issue.get("repository")
        pr_repo = issue_repo
        if isinstance(repository, dict):
            repo_name = repository.get("full_name")
            if repo_name:
                pr_repo = str(repo_name)

        linked_at = _parse_datetime(event.get("created_at") or event.get("updated_at"))
        if linked_at is None:
            continue

        source_event_id_raw = event.get("id")
        source_event_id = None
        if source_event_id_raw is not None:
            try:
                source_event_id = int(source_event_id_raw)
            except (TypeError, ValueError):
                source_event_id = None

        link_row = FlakyIssuePrLinkRow(
            issue_repo=issue_repo,
            issue_number=issue_number,
            pr_repo=pr_repo,
            pr_number=normalized_pr_number,
            pr_url=f"https://github.com/{pr_repo}/pull/{normalized_pr_number}",
            pr_title=str(source_issue.get("title") or ""),
            link_type="linked_pull_request",
            source_event_type="cross-referenced",
            source_event_id=source_event_id,
            linked_at=linked_at,
            source_ticket_updated_at=source_ticket_updated_at,
        )

        existing = links_by_pr.get((pr_repo, normalized_pr_number))
        if existing is None or link_row.linked_at < existing.linked_at:
            links_by_pr[(pr_repo, normalized_pr_number)] = link_row

    return sorted(
        links_by_pr.values(),
        key=lambda item: (item.issue_repo, item.issue_number, item.pr_repo, item.pr_number),
    )


def _parse_timeline(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    raise ValueError(f"Unsupported issue timeline payload: {value!r}")


def _extract_issue_lifecycle(
    timeline: list[dict[str, Any]],
) -> tuple[datetime | None, datetime | None, int]:
    issue_closed_at: datetime | None = None
    last_reopened_at: datetime | None = None
    reopen_count = 0

    for event in timeline:
        event_name = str(event.get("event") or "")
        created_at = event.get("created_at") or event.get("createdAt")
        if not created_at:
            continue
        event_time = _parse_datetime(created_at)
        if event_name == "closed":
            issue_closed_at = event_time
        elif event_name == "reopened":
            last_reopened_at = event_time
            reopen_count += 1

    return issue_closed_at, last_reopened_at, reopen_count


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Unsupported datetime value: {value!r}")


def _normalize_datetime_key(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _upsert_flaky_issues(connection: Connection, rows: list[FlakyIssueRow]) -> None:
    if not rows:
        return
    statement = _build_upsert_statement(connection)
    connection.execute(statement, [_row_to_params(row) for row in rows])


def _replace_flaky_issue_pr_links(
    connection: Connection,
    *,
    issue_keys: list[tuple[str, int]],
    rows: list[FlakyIssuePrLinkRow],
) -> None:
    if not issue_keys:
        return

    _delete_flaky_issue_pr_links_for_issues(connection, issue_keys)
    if rows:
        statement = _build_issue_pr_links_upsert_statement(connection)
        connection.execute(statement, [_issue_pr_link_to_params(row) for row in rows])


def _row_to_params(row: FlakyIssueRow) -> dict[str, Any]:
    return {
        "repo": row.repo,
        "issue_number": row.issue_number,
        "issue_url": row.issue_url,
        "issue_title": row.issue_title,
        "case_name": row.case_name,
        "issue_status": row.issue_status,
        "issue_branch": row.issue_branch,
        "branch_source": row.branch_source,
        "issue_created_at": row.issue_created_at,
        "issue_updated_at": row.issue_updated_at,
        "issue_closed_at": row.issue_closed_at,
        "last_reopened_at": row.last_reopened_at,
        "reopen_count": row.reopen_count,
        "source_ticket_id": row.source_ticket_id,
        "source_ticket_updated_at": row.source_ticket_updated_at,
    }


def _issue_pr_link_to_params(row: FlakyIssuePrLinkRow) -> dict[str, Any]:
    return {
        "issue_repo": row.issue_repo,
        "issue_number": row.issue_number,
        "pr_repo": row.pr_repo,
        "pr_number": row.pr_number,
        "pr_url": row.pr_url,
        "pr_title": row.pr_title,
        "link_type": row.link_type,
        "source_event_type": row.source_event_type,
        "source_event_id": row.source_event_id,
        "linked_at": row.linked_at,
        "source_ticket_updated_at": row.source_ticket_updated_at,
    }


def _delete_flaky_issue_pr_links_for_issues(
    connection: Connection,
    issue_keys: list[tuple[str, int]],
) -> None:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (issue_repo, issue_number) in enumerate(issue_keys):
        clauses.append(
            f"(issue_repo = :issue_repo_{index} AND issue_number = :issue_number_{index})"
        )
        params[f"issue_repo_{index}"] = issue_repo
        params[f"issue_number_{index}"] = issue_number

    connection.execute(
        text(
            f"""
            DELETE FROM ci_l1_flaky_issue_pr_links
            WHERE {' OR '.join(clauses)}
            """
        ),
        params,
    )


def _build_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
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
              :repo,
              :issue_number,
              :issue_url,
              :issue_title,
              :case_name,
              :issue_status,
              :issue_branch,
              :branch_source,
              :issue_created_at,
              :issue_updated_at,
              :issue_closed_at,
              :last_reopened_at,
              :reopen_count,
              :source_ticket_id,
              :source_ticket_updated_at,
              CURRENT_TIMESTAMP,
              CURRENT_TIMESTAMP
            )
            ON CONFLICT(repo, issue_number) DO UPDATE SET
              issue_url = excluded.issue_url,
              issue_title = excluded.issue_title,
              case_name = excluded.case_name,
              issue_status = excluded.issue_status,
              issue_branch = excluded.issue_branch,
              branch_source = excluded.branch_source,
              issue_created_at = excluded.issue_created_at,
              issue_updated_at = excluded.issue_updated_at,
              issue_closed_at = excluded.issue_closed_at,
              last_reopened_at = excluded.last_reopened_at,
              reopen_count = excluded.reopen_count,
              source_ticket_id = excluded.source_ticket_id,
              source_ticket_updated_at = excluded.source_ticket_updated_at,
              updated_at = CURRENT_TIMESTAMP
            """
        )

    return text(
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
          source_ticket_updated_at
        ) VALUES (
          :repo,
          :issue_number,
          :issue_url,
          :issue_title,
          :case_name,
          :issue_status,
          :issue_branch,
          :branch_source,
          :issue_created_at,
          :issue_updated_at,
          :issue_closed_at,
          :last_reopened_at,
          :reopen_count,
          :source_ticket_id,
          :source_ticket_updated_at
        )
        ON DUPLICATE KEY UPDATE
          issue_url = VALUES(issue_url),
          issue_title = VALUES(issue_title),
          case_name = VALUES(case_name),
          issue_status = VALUES(issue_status),
          issue_branch = VALUES(issue_branch),
          branch_source = VALUES(branch_source),
          issue_created_at = VALUES(issue_created_at),
          issue_updated_at = VALUES(issue_updated_at),
          issue_closed_at = VALUES(issue_closed_at),
          last_reopened_at = VALUES(last_reopened_at),
          reopen_count = VALUES(reopen_count),
          source_ticket_id = VALUES(source_ticket_id),
          source_ticket_updated_at = VALUES(source_ticket_updated_at),
          updated_at = CURRENT_TIMESTAMP
        """
    )


def _build_issue_pr_links_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
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
              source_ticket_updated_at,
              created_at,
              updated_at
            ) VALUES (
              :issue_repo,
              :issue_number,
              :pr_repo,
              :pr_number,
              :pr_url,
              :pr_title,
              :link_type,
              :source_event_type,
              :source_event_id,
              :linked_at,
              :source_ticket_updated_at,
              CURRENT_TIMESTAMP,
              CURRENT_TIMESTAMP
            )
            ON CONFLICT(issue_repo, issue_number, pr_repo, pr_number) DO UPDATE SET
              pr_url = excluded.pr_url,
              pr_title = excluded.pr_title,
              link_type = excluded.link_type,
              source_event_type = excluded.source_event_type,
              source_event_id = excluded.source_event_id,
              linked_at = excluded.linked_at,
              source_ticket_updated_at = excluded.source_ticket_updated_at,
              updated_at = CURRENT_TIMESTAMP
            """
        )

    return text(
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
          :issue_repo,
          :issue_number,
          :pr_repo,
          :pr_number,
          :pr_url,
          :pr_title,
          :link_type,
          :source_event_type,
          :source_event_id,
          :linked_at,
          :source_ticket_updated_at
        )
        ON DUPLICATE KEY UPDATE
          pr_url = VALUES(pr_url),
          pr_title = VALUES(pr_title),
          link_type = VALUES(link_type),
          source_event_type = VALUES(source_event_type),
          source_event_id = VALUES(source_event_id),
          linked_at = VALUES(linked_at),
          source_ticket_updated_at = VALUES(source_ticket_updated_at),
          updated_at = CURRENT_TIMESTAMP
        """
    )
