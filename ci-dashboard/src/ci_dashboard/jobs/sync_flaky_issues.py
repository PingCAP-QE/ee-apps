from __future__ import annotations

import html
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import SyncFlakyIssuesSummary
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

        prepared_rows: list[FlakyIssueRow] = []
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

            prepared_rows.append(
                _build_flaky_issue_row(
                    row,
                    issue_branch=issue_branch,
                    branch_source=branch_source,
                )
            )

        for batch in chunked(prepared_rows, settings.jobs.batch_size):
            with engine.begin() as connection:
                _upsert_flaky_issues(connection, batch)
                summary.rows_written += len(batch)

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


def fetch_issue_body_via_gh(*, repo: str, issue_number: int) -> str:
    gh_path = shutil.which("gh")
    if gh_path is None:
        raise RuntimeError("gh CLI is not available in PATH")

    result = subprocess.run(
        [
            gh_path,
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "body",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    body = payload.get("body")
    return str(body) if body is not None else ""


def parse_issue_branch(issue_body: str) -> str | None:
    normalized = html.unescape(issue_body).replace("\r\n", "\n").replace("\\n", "\n")
    match = BRANCH_PATTERN.search(normalized)
    if match is None:
        return None
    return match.group(1).strip() or None


def _resolve_issue_branch(
    row: dict[str, Any],
    existing: dict[str, Any] | None,
) -> tuple[str | None, str, bool, bool]:
    ticket_body = str(row.get("body") or "")
    body_branch = parse_issue_branch(ticket_body)
    if body_branch:
        return body_branch, "ticket_body", False, False

    repo = str(row["repo"])
    issue_number = int(row["number"])
    try:
        gh_body = fetch_issue_body_via_gh(repo=repo, issue_number=issue_number)
        gh_branch = parse_issue_branch(gh_body)
        if gh_branch:
            return gh_branch, "gh_cli_body", True, False
        fallback_branch, fallback_source = _fallback_issue_branch(existing)
        return fallback_branch, fallback_source, True, False
    except Exception as exc:
        LOG.warning(
            "failed to fetch flaky issue body via gh",
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
            SELECT repo, issue_number, issue_branch, branch_source
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


def _upsert_flaky_issues(connection: Connection, rows: list[FlakyIssueRow]) -> None:
    if not rows:
        return
    statement = _build_upsert_statement(connection)
    connection.execute(statement, [_row_to_params(row) for row in rows])


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
