from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import SyncPrEventsSummary
from ci_dashboard.common.sql_helpers import chunked
from ci_dashboard.jobs.retest_parser import is_supported_retest_command
from ci_dashboard.jobs.state_store import (
    get_job_state,
    mark_job_failed,
    mark_job_started,
    mark_job_succeeded,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "ci-sync-pr-events"

SELECT_NEW_BUILD_LINKED_PRS = text(
    """
    SELECT DISTINCT repo_full_name, pr_number
    FROM ci_l1_builds
    WHERE is_pr_build = 1
      AND pr_number IS NOT NULL
      AND source_prow_row_id > :last_build_source_prow_row_id_seen
    """
)

SELECT_UPDATED_TRACKED_PRS = text(
    """
    SELECT DISTINCT g.repo, g.number
    FROM github_tickets g
    JOIN (
      SELECT DISTINCT repo, pr_number
      FROM ci_l1_pr_events
    ) tracked
      ON tracked.repo = g.repo
     AND tracked.pr_number = g.number
    WHERE g.type = 'pull'
      AND g.updated_at > :last_ticket_updated_at
    """
)


def _build_select_window_build_linked_prs_query(has_end: bool):
    end_clause = ""
    if has_end:
        end_clause = "  AND start_time < :start_time_to\n"
    return text(
        f"""
        SELECT DISTINCT repo_full_name, pr_number
        FROM ci_l1_builds
        WHERE is_pr_build = 1
          AND pr_number IS NOT NULL
          AND start_time >= :start_time_from
{end_clause}        """
    )


def run_sync_pr_events(engine: Engine, settings: Settings) -> SyncPrEventsSummary:
    with engine.begin() as connection:
        watermark = _load_watermark(connection)
        mark_job_started(connection, JOB_NAME, watermark)

    summary = SyncPrEventsSummary()

    try:
        with engine.begin() as connection:
            candidate_prs = _get_candidate_prs(connection, watermark)
            build_watermark = _load_build_watermark(connection)

        summary.candidate_prs = len(candidate_prs)
        summary.last_build_source_prow_row_id_seen = build_watermark
        max_ticket_updated_at = watermark.get("last_ticket_updated_at")

        for batch in chunked(sorted(candidate_prs), settings.jobs.batch_size):
            with engine.begin() as connection:
                tickets = _fetch_ticket_rows(connection, batch)
                summary.batches_processed += 1
                summary.ticket_rows_fetched += len(tickets)

                event_rows: list[dict[str, Any]] = []
                for ticket in tickets:
                    repo = str(ticket["repo"])
                    pr_number = int(ticket["number"])
                    branch_meta = _extract_branch_metadata(ticket)

                    snapshot = _build_snapshot_event(ticket, repo, pr_number, branch_meta)
                    if snapshot is not None:
                        event_rows.append(snapshot)

                    event_rows.extend(
                        _parse_timeline(ticket.get("timeline"), repo, pr_number, branch_meta)
                    )

                    updated_at = ticket.get("updated_at")
                    if updated_at is not None:
                        ticket_updated_at = str(updated_at)
                        if max_ticket_updated_at is None or ticket_updated_at > max_ticket_updated_at:
                            max_ticket_updated_at = ticket_updated_at

                if event_rows:
                    _upsert_pr_events(connection, event_rows)
                    summary.events_written += len(event_rows)

        new_watermark = {
            "last_build_source_prow_row_id_seen": build_watermark,
            "last_ticket_updated_at": max_ticket_updated_at,
        }
        with engine.begin() as connection:
            mark_job_succeeded(connection, JOB_NAME, new_watermark)

        summary.last_ticket_updated_at = max_ticket_updated_at
        return summary
    except Exception as exc:
        with engine.begin() as connection:
            mark_job_failed(connection, JOB_NAME, watermark, str(exc))
        raise


def run_sync_pr_events_for_time_window(
    engine: Engine,
    settings: Settings,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
) -> SyncPrEventsSummary:
    summary = SyncPrEventsSummary()

    with engine.begin() as connection:
        candidate_prs = _get_window_candidate_prs(
            connection,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )

    summary.candidate_prs = len(candidate_prs)
    max_ticket_updated_at: str | None = None

    for batch in chunked(sorted(candidate_prs), settings.jobs.batch_size):
        with engine.begin() as connection:
            tickets = _fetch_ticket_rows(connection, batch)
            summary.batches_processed += 1
            summary.ticket_rows_fetched += len(tickets)

            event_rows: list[dict[str, Any]] = []
            for ticket in tickets:
                repo = str(ticket["repo"])
                pr_number = int(ticket["number"])
                branch_meta = _extract_branch_metadata(ticket)

                snapshot = _build_snapshot_event(ticket, repo, pr_number, branch_meta)
                if snapshot is not None:
                    event_rows.append(snapshot)

                event_rows.extend(_parse_timeline(ticket.get("timeline"), repo, pr_number, branch_meta))

                updated_at = ticket.get("updated_at")
                if updated_at is not None:
                    ticket_updated_at = str(updated_at)
                    if max_ticket_updated_at is None or ticket_updated_at > max_ticket_updated_at:
                        max_ticket_updated_at = ticket_updated_at

            if event_rows:
                _upsert_pr_events(connection, event_rows)
                summary.events_written += len(event_rows)

    summary.last_ticket_updated_at = max_ticket_updated_at
    return summary


def _load_watermark(connection: Connection) -> dict[str, Any]:
    state = get_job_state(connection, JOB_NAME)
    if state is None:
        return {
            "last_build_source_prow_row_id_seen": 0,
            "last_ticket_updated_at": None,
        }
    return {
        "last_build_source_prow_row_id_seen": int(
            state.watermark.get("last_build_source_prow_row_id_seen", 0) or 0
        ),
        "last_ticket_updated_at": state.watermark.get("last_ticket_updated_at"),
    }


def _load_build_watermark(connection: Connection) -> int:
    result = connection.execute(
        text(
            """
            SELECT COALESCE(MAX(source_prow_row_id), 0)
            FROM ci_l1_builds
            WHERE is_pr_build = 1
            """
        )
    )
    return int(result.scalar_one())


def _get_candidate_prs(
    connection: Connection,
    watermark: Mapping[str, Any],
) -> set[tuple[str, int]]:
    candidates: set[tuple[str, int]] = set()

    rows = connection.execute(
        SELECT_NEW_BUILD_LINKED_PRS,
        {
            "last_build_source_prow_row_id_seen": watermark["last_build_source_prow_row_id_seen"],
        },
    ).mappings()
    for row in rows:
        candidates.add((str(row["repo_full_name"]), int(row["pr_number"])))

    last_ticket_updated_at = watermark.get("last_ticket_updated_at")
    if last_ticket_updated_at:
        rows = connection.execute(
            SELECT_UPDATED_TRACKED_PRS,
            {"last_ticket_updated_at": last_ticket_updated_at},
        ).mappings()
        for row in rows:
            candidates.add((str(row["repo"]), int(row["number"])))

    return candidates


def _get_window_candidate_prs(
    connection: Connection,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
) -> set[tuple[str, int]]:
    params: dict[str, Any] = {
        "start_time_from": start_time_from,
    }
    if start_time_to is not None:
        params["start_time_to"] = start_time_to

    rows = connection.execute(
        _build_select_window_build_linked_prs_query(start_time_to is not None),
        params,
    ).mappings()

    return {
        (str(row["repo_full_name"]), int(row["pr_number"]))
        for row in rows
    }


def _fetch_ticket_rows(
    connection: Connection,
    pr_keys: list[tuple[str, int]],
) -> list[Mapping[str, Any]]:
    if not pr_keys:
        return []

    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (repo, pr_number) in enumerate(pr_keys):
        clauses.append(f"(repo = :repo_{index} AND number = :pr_number_{index})")
        params[f"repo_{index}"] = repo
        params[f"pr_number_{index}"] = pr_number

    query = text(
        f"""
        SELECT repo, number, created_at, updated_at, timeline, branches
        FROM github_tickets
        WHERE type = 'pull'
          AND ({' OR '.join(clauses)})
        """
    )
    return list(connection.execute(query, params).mappings())


def _build_snapshot_event(
    ticket: Mapping[str, Any],
    repo: str,
    pr_number: int,
    branch_meta: Mapping[str, Any],
) -> dict[str, Any] | None:
    event_time = _parse_datetime(ticket.get("updated_at")) or _parse_datetime(ticket.get("created_at"))
    if event_time is None:
        return None
    return {
        "repo": repo,
        "pr_number": pr_number,
        "event_key": "pr_snapshot",
        "event_time": event_time,
        "event_type": "pr_snapshot",
        "actor_login": None,
        "comment_id": None,
        "comment_body": None,
        "retest_event": 0,
        "commit_sha": None,
        **branch_meta,
    }


def _extract_branch_metadata(ticket: Mapping[str, Any]) -> dict[str, Any]:
    branches_raw = ticket.get("branches")
    target_branch = None
    head_ref = None
    head_sha = None

    if branches_raw:
        try:
            branches = json.loads(branches_raw) if isinstance(branches_raw, str) else branches_raw
        except (TypeError, json.JSONDecodeError):
            branches = None
        if isinstance(branches, dict):
            base = branches.get("base")
            head = branches.get("head")
            if isinstance(base, dict):
                target_branch = _coerce_str(base.get("ref"))
            if isinstance(head, dict):
                head_ref = _coerce_str(head.get("ref"))
                head_sha = _coerce_str(head.get("sha"))

    return {
        "target_branch": target_branch,
        "head_ref": head_ref,
        "head_sha": head_sha,
    }


def _parse_timeline(
    timeline_raw: Any,
    repo: str,
    pr_number: int,
    branch_meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not timeline_raw:
        return []

    try:
        timeline = json.loads(timeline_raw) if isinstance(timeline_raw, str) else timeline_raw
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(timeline, list):
        return []

    events: list[dict[str, Any]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue

        event_name = item.get("event")
        if event_name == "committed":
            commit_sha = _coerce_str(item.get("sha"))
            event_time = _parse_datetime(
                _get_nested(item, "committer", "date")
                or _get_nested(item, "author", "date")
                or item.get("created_at")
            )
            if event_time is None:
                continue
            event_key = (
                f"committed:{commit_sha}:{event_time.isoformat()}"
                if commit_sha
                else _hash_key("committed", str(pr_number), event_time.isoformat())
            )
            events.append(
                {
                    "repo": repo,
                    "pr_number": pr_number,
                    "event_key": event_key,
                    "event_time": event_time,
                    "event_type": "committed",
                    "actor_login": _coerce_str(_get_nested(item, "author", "name"))
                    or _coerce_str(_get_nested(item, "author", "login")),
                    "comment_id": None,
                    "comment_body": None,
                    "retest_event": 0,
                    "commit_sha": commit_sha[:64] if commit_sha else None,
                    **branch_meta,
                }
            )
            continue

        if event_name != "commented":
            continue

        body = _coerce_str(item.get("body"))
        if not is_supported_retest_command(body):
            continue

        event_time = _parse_datetime(item.get("created_at"))
        if event_time is None:
            continue

        comment_id = item.get("id")
        event_key = (
            f"retest_comment:{comment_id}"
            if comment_id is not None
            else _hash_key("retest_comment", body or "", event_time.isoformat())
        )
        events.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "event_key": event_key,
                "event_time": event_time,
                "event_type": "retest_comment",
                "actor_login": _coerce_str(_get_nested(item, "user", "login")),
                "comment_id": int(comment_id) if comment_id is not None else None,
                "comment_body": body,
                "retest_event": 1,
                "commit_sha": None,
                **branch_meta,
            }
        )

    return events


def _upsert_pr_events(connection: Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    columns = (
        "repo",
        "pr_number",
        "event_key",
        "event_time",
        "event_type",
        "actor_login",
        "comment_id",
        "comment_body",
        "retest_event",
        "commit_sha",
        "target_branch",
        "head_ref",
        "head_sha",
    )
    value_list = ", ".join(f":{column}" for column in columns)

    if connection.dialect.name == "sqlite":
        assignments = ",\n      ".join(
            f"{column} = excluded.{column}" for column in columns if column not in {"repo", "pr_number", "event_key"}
        )
        statement = text(
            f"""
            INSERT INTO ci_l1_pr_events (
              {", ".join(columns)}
            ) VALUES (
              {value_list}
            )
            ON CONFLICT(repo, pr_number, event_key) DO UPDATE SET
              {assignments},
              updated_at = CURRENT_TIMESTAMP
            """
        )
    else:
        assignments = ",\n      ".join(
            f"{column} = VALUES({column})" for column in columns if column not in {"repo", "pr_number", "event_key"}
        )
        statement = text(
            f"""
            INSERT INTO ci_l1_pr_events (
              {", ".join(columns)}
            ) VALUES (
              {value_list}
            )
            ON DUPLICATE KEY UPDATE
              {assignments},
              updated_at = CURRENT_TIMESTAMP
            """
        )

    connection.execute(statement, rows)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_naive_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        return _to_naive_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    return None


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _get_nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _hash_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
