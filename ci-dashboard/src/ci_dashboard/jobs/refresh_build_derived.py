from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import RefreshBuildDerivedSummary
from ci_dashboard.common.sql_helpers import chunked
from ci_dashboard.jobs.build_url_matcher import classify_cloud_phase, normalize_build_url
from ci_dashboard.jobs.flaky import BuildAttempt, compute_group_flags, parse_datetime
from ci_dashboard.jobs.state_store import (
    get_job_state,
    mark_job_failed,
    mark_job_started,
    mark_job_succeeded,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "ci-refresh-build-derived"
REQUIRE_RETEST_FOR_RETRY_LOOP = False
DEFAULT_WRITE_BATCH_SIZE = 100
DEFAULT_REFRESH_BUILD_LIMIT = 5000
CASE_RUN_DERIVED_WATERMARK_KEY = "last_processed_case_run_derived_id"


@dataclass(frozen=True)
class ImpactedBuildSelection:
    build_ids: list[int]
    has_more: bool


def run_refresh_build_derived(
    engine: Engine,
    settings: Settings | None = None,
) -> RefreshBuildDerivedSummary:
    with engine.begin() as connection:
        watermark = _load_watermark(connection)
        mark_job_started(connection, JOB_NAME, watermark)

    summary = RefreshBuildDerivedSummary()
    chunk_size = _resolve_chunk_size(settings)
    group_chunk_size = _resolve_refresh_group_chunk_size(settings)
    write_batch_size = _resolve_write_batch_size(settings)
    refresh_build_limit = _resolve_refresh_build_limit(settings)

    try:
        with engine.begin() as connection:
            case_run_derived_watermark = _refresh_problem_case_run_derived_columns(
                connection,
                last_processed_case_run_derived_id=_normalize_optional_int(
                    watermark.get(CASE_RUN_DERIVED_WATERMARK_KEY),
                    default=0,
                ),
                batch_size=chunk_size,
                write_batch_size=write_batch_size,
            )
            selection_watermarks = _resolve_selection_watermarks(connection, watermark)
            impacted_selection = _get_impacted_build_ids(
                connection,
                watermark,
                selection_watermarks,
                max_builds=refresh_build_limit,
            )
            impacted_build_ids = impacted_selection.build_ids

        summary.impacted_builds = len(impacted_build_ids)

        if impacted_build_ids:
            _apply_refresh_phases(
                engine,
                impacted_build_ids,
                summary,
                chunk_size=chunk_size,
                group_chunk_size=group_chunk_size,
                write_batch_size=write_batch_size,
            )

        next_watermark = _build_success_watermark(
            watermark,
            selection_watermarks,
            impacted_selection,
            case_run_derived_watermark=case_run_derived_watermark,
        )
        summary.last_processed_build_id = int(next_watermark["last_processed_build_id"] or 0)
        summary.last_processed_pr_event_updated_at = _normalize_optional_string(
            next_watermark.get("last_processed_pr_event_updated_at")
        )
        summary.last_processed_case_report_time = _normalize_optional_string(
            next_watermark.get("last_processed_case_report_time")
        )

        with engine.begin() as connection:
            mark_job_succeeded(connection, JOB_NAME, next_watermark)

        return summary
    except Exception as exc:
        with engine.begin() as connection:
            mark_job_failed(connection, JOB_NAME, watermark, str(exc))
        raise


def run_refresh_build_derived_for_time_window(
    engine: Engine,
    settings: Settings | None = None,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
) -> RefreshBuildDerivedSummary:
    summary = RefreshBuildDerivedSummary()
    chunk_size = _resolve_chunk_size(settings)
    group_chunk_size = _resolve_refresh_group_chunk_size(settings)
    write_batch_size = _resolve_write_batch_size(settings)

    with engine.begin() as connection:
        impacted_build_ids = _get_build_ids_in_time_window(
            connection,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )

    summary.impacted_builds = len(impacted_build_ids)
    summary.last_processed_build_id = max(impacted_build_ids, default=0)

    if impacted_build_ids:
        _apply_refresh_phases(
            engine,
            impacted_build_ids,
            summary,
            chunk_size=chunk_size,
            group_chunk_size=group_chunk_size,
            write_batch_size=write_batch_size,
        )

    return summary


def run_refresh_flaky_signals_for_time_window(
    engine: Engine,
    settings: Settings | None = None,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
) -> RefreshBuildDerivedSummary:
    summary = RefreshBuildDerivedSummary()
    chunk_size = _resolve_chunk_size(settings)
    group_chunk_size = _resolve_refresh_group_chunk_size(settings)
    write_batch_size = _resolve_write_batch_size(settings)

    with engine.begin() as connection:
        impacted_build_ids = _get_build_ids_in_time_window(
            connection,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )

    summary.impacted_builds = len(impacted_build_ids)
    summary.last_processed_build_id = max(impacted_build_ids, default=0)

    if not impacted_build_ids:
        return summary

    updated_build_ids = _apply_flaky_flags_in_chunks(
        engine,
        impacted_build_ids=impacted_build_ids,
        group_chunk_size=group_chunk_size,
        write_batch_size=write_batch_size,
        summary=summary,
    )

    _apply_build_phase_in_chunks(
        engine,
        phase_name="failure category",
        build_ids=sorted(updated_build_ids),
        chunk_size=chunk_size,
        worker=lambda connection, build_ids: _phase_d_failure_category(
            connection,
            build_ids,
            write_batch_size=write_batch_size,
        ),
        assign=lambda rows_updated: _assign_failure_category_rows_updated(summary, rows_updated),
    )

    return summary


def _load_watermark(connection: Connection) -> dict[str, Any]:
    state = get_job_state(connection, JOB_NAME)
    if state is None:
        return {
            "last_processed_build_id": 0,
            "last_processed_pr_event_updated_at": None,
            "last_processed_case_report_time": None,
            CASE_RUN_DERIVED_WATERMARK_KEY: 0,
            "pending_refresh": False,
            "pending_target_build_id": None,
            "pending_target_pr_event_updated_at": None,
            "pending_target_case_report_time": None,
        }
    try:
        return {
            "last_processed_build_id": _normalize_optional_int(
                state.watermark.get("last_processed_build_id"),
                default=0,
            ),
            "last_processed_pr_event_updated_at": _normalize_optional_string(
                state.watermark.get("last_processed_pr_event_updated_at")
            ),
            "last_processed_case_report_time": _normalize_optional_string(
                state.watermark.get("last_processed_case_report_time")
            ),
            CASE_RUN_DERIVED_WATERMARK_KEY: _normalize_optional_int(
                state.watermark.get(CASE_RUN_DERIVED_WATERMARK_KEY),
                default=0,
            ),
            "pending_refresh": bool(state.watermark.get("pending_refresh", False)),
            "pending_target_build_id": _normalize_optional_int(
                state.watermark.get("pending_target_build_id")
            ),
            "pending_target_pr_event_updated_at": _normalize_optional_string(
                state.watermark.get("pending_target_pr_event_updated_at")
            ),
            "pending_target_case_report_time": _normalize_optional_string(
                state.watermark.get("pending_target_case_report_time")
            ),
        }
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {JOB_NAME} watermark: {state.watermark!r}") from exc


def _load_selection_watermarks(connection: Connection) -> dict[str, Any]:
    build_id = connection.execute(text("SELECT COALESCE(MAX(id), 0) FROM ci_l1_builds")).scalar_one()
    pr_event_updated_at = connection.execute(
        text("SELECT MAX(updated_at) FROM ci_l1_pr_events")
    ).scalar_one()
    case_report_time = connection.execute(
        text("SELECT MAX(report_time) FROM problem_case_runs")
    ).scalar_one()

    return {
        "last_processed_build_id": int(build_id or 0),
        "last_processed_pr_event_updated_at": str(pr_event_updated_at)
        if pr_event_updated_at is not None
        else None,
        "last_processed_case_report_time": str(case_report_time) if case_report_time is not None else None,
    }


def _resolve_selection_watermarks(
    connection: Connection,
    watermark: Mapping[str, Any],
) -> dict[str, Any]:
    if not bool(watermark.get("pending_refresh", False)):
        return _load_selection_watermarks(connection)

    return {
        "last_processed_build_id": _normalize_optional_int(
            watermark.get("pending_target_build_id"),
            default=_normalize_optional_int(watermark.get("last_processed_build_id"), default=0),
        ),
        "last_processed_pr_event_updated_at": _normalize_optional_string(
            watermark.get("pending_target_pr_event_updated_at")
        ),
        "last_processed_case_report_time": _normalize_optional_string(
            watermark.get("pending_target_case_report_time")
        ),
    }


def _get_impacted_build_ids(
    connection: Connection,
    watermark: Mapping[str, Any],
    selection_watermarks: Mapping[str, Any],
    *,
    max_builds: int,
) -> ImpactedBuildSelection:
    impacted_ids: set[int] = set()
    selection_limit = max_builds + 1
    pending_refresh = bool(watermark.get("pending_refresh", False))
    last_processed_build_id = _normalize_optional_int(
        watermark.get("last_processed_build_id"),
        default=0,
    )
    has_more = False

    new_build_ids = _fetch_new_build_ids(
        connection,
        lower_build_id=last_processed_build_id,
        upper_build_id=_normalize_optional_int(
            selection_watermarks.get("last_processed_build_id"),
            default=last_processed_build_id,
        ),
        selection_limit=selection_limit,
    )
    if len(new_build_ids) == selection_limit:
        has_more = True
    impacted_ids.update(new_build_ids)

    pr_event_build_ids = _fetch_pr_event_impacted_build_ids(
        connection,
        last_processed_pr_event_updated_at=_normalize_optional_string(
            watermark.get("last_processed_pr_event_updated_at")
        ),
        target_pr_event_updated_at=_normalize_optional_string(
            selection_watermarks.get("last_processed_pr_event_updated_at")
        ),
        upper_build_id=_normalize_optional_int(
            selection_watermarks.get("last_processed_build_id"),
            default=last_processed_build_id,
        ),
        after_build_id=last_processed_build_id if pending_refresh else 0,
        selection_limit=selection_limit,
    )
    if len(pr_event_build_ids) == selection_limit:
        has_more = True
    impacted_ids.update(pr_event_build_ids)

    case_build_ids = _fetch_case_impacted_build_ids(
        connection,
        last_processed_case_report_time=_normalize_optional_string(
            watermark.get("last_processed_case_report_time")
        ),
        target_case_report_time=_normalize_optional_string(
            selection_watermarks.get("last_processed_case_report_time")
        ),
        upper_build_id=_normalize_optional_int(
            selection_watermarks.get("last_processed_build_id"),
            default=last_processed_build_id,
        ),
        after_build_id=last_processed_build_id if pending_refresh else 0,
        selection_limit=selection_limit,
    )
    if len(case_build_ids) == selection_limit:
        has_more = True
    impacted_ids.update(case_build_ids)

    build_ids = sorted(impacted_ids)
    if len(build_ids) > max_builds:
        has_more = True
        build_ids = build_ids[:max_builds]

    return ImpactedBuildSelection(build_ids=build_ids, has_more=has_more)


def _get_build_ids_in_time_window(
    connection: Connection,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
) -> list[int]:
    if start_time_to is None:
        query = text(
            """
            SELECT id
            FROM ci_l1_builds
            WHERE start_time >= :start_time_from
            ORDER BY id
            """
        )
        params = {"start_time_from": start_time_from}
    else:
        query = text(
            """
            SELECT id
            FROM ci_l1_builds
            WHERE start_time >= :start_time_from
              AND start_time < :start_time_to
            ORDER BY id
            """
        )
        params = {
            "start_time_from": start_time_from,
            "start_time_to": start_time_to,
        }

    rows = connection.execute(query, params).mappings()
    return [int(row["id"]) for row in rows]


def _apply_refresh_phases(
    engine: Engine,
    impacted_build_ids: list[int],
    summary: RefreshBuildDerivedSummary,
    *,
    chunk_size: int,
    group_chunk_size: int,
    write_batch_size: int,
) -> None:
    _apply_build_phase_in_chunks(
        engine,
        phase_name="branch enrichment",
        build_ids=impacted_build_ids,
        chunk_size=chunk_size,
        worker=lambda connection, build_ids: _phase_a_branch_enrichment(
            connection,
            build_ids,
            write_batch_size=write_batch_size,
        ),
        assign=lambda rows_updated: _assign_branch_rows_updated(summary, rows_updated),
    )

    updated_build_ids = _apply_flaky_flags_in_chunks(
        engine,
        impacted_build_ids=impacted_build_ids,
        group_chunk_size=group_chunk_size,
        write_batch_size=write_batch_size,
        summary=summary,
    )

    _apply_build_phase_in_chunks(
        engine,
        phase_name="case evidence",
        build_ids=impacted_build_ids,
        chunk_size=chunk_size,
        worker=lambda connection, build_ids: _phase_c_case_evidence(
            connection,
            build_ids,
            write_batch_size=write_batch_size,
        ),
        assign=lambda rows_updated: _assign_case_match_rows_updated(summary, rows_updated),
    )

    failure_category_ids = sorted(set(impacted_build_ids).union(updated_build_ids))
    _apply_build_phase_in_chunks(
        engine,
        phase_name="failure category",
        build_ids=failure_category_ids,
        chunk_size=chunk_size,
        worker=lambda connection, build_ids: _phase_d_failure_category(
            connection,
            build_ids,
            write_batch_size=write_batch_size,
        ),
        assign=lambda rows_updated: _assign_failure_category_rows_updated(summary, rows_updated),
    )


def _apply_build_phase_in_chunks(
    engine: Engine,
    *,
    phase_name: str,
    build_ids: list[int],
    chunk_size: int,
    worker,
    assign,
) -> None:
    total_chunks = _chunk_count(build_ids, chunk_size)
    for chunk_index, build_id_chunk in enumerate(chunked(build_ids, chunk_size), start=1):
        with engine.begin() as connection:
            rows_updated = worker(connection, build_id_chunk)
        assign(rows_updated)
        LOG.info(
            "%s chunk %s/%s processed %s build ids and updated %s rows",
            phase_name,
            chunk_index,
            total_chunks,
            len(build_id_chunk),
            rows_updated,
        )


def _apply_flaky_flags_in_chunks(
    engine: Engine,
    *,
    impacted_build_ids: list[int],
    group_chunk_size: int,
    write_batch_size: int,
    summary: RefreshBuildDerivedSummary,
) -> set[int]:
    groups = _collect_impacted_groups(engine, impacted_build_ids, group_chunk_size)
    if not groups:
        return set()

    total_chunks = _chunk_count(groups, group_chunk_size)
    updated_build_ids: set[int] = set()
    for chunk_index, group_chunk in enumerate(chunked(groups, group_chunk_size), start=1):
        with engine.begin() as connection:
            flag_update = _phase_b_flaky_flags_for_groups(
                connection,
                group_chunk,
                write_batch_size=write_batch_size,
            )
        summary.groups_recomputed += flag_update["groups_recomputed"]
        summary.flaky_rows_updated += flag_update["rows_updated"]
        updated_build_ids.update(flag_update["updated_build_ids"])
        LOG.info(
            "flaky flag chunk %s/%s processed %s groups and updated %s rows",
            chunk_index,
            total_chunks,
            len(group_chunk),
            flag_update["rows_updated"],
        )

    return updated_build_ids


def _collect_impacted_groups(
    engine: Engine,
    impacted_build_ids: list[int],
    chunk_size: int,
) -> list[dict[str, Any]]:
    seen_group_keys: set[tuple[str, int, str]] = set()
    groups: list[dict[str, Any]] = []
    for build_id_chunk in chunked(impacted_build_ids, chunk_size):
        with engine.begin() as connection:
            rows = list(_fetch_impacted_groups(connection, build_id_chunk))
        for row in rows:
            group = _normalize_group(row)
            group_key = _group_key(group)
            if group_key in seen_group_keys:
                continue
            seen_group_keys.add(group_key)
            groups.append(group)
    return groups


def _normalize_group(group: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "repo_full_name": str(group["repo_full_name"]),
        "pr_number": int(group["pr_number"]),
        "job_name": str(group["job_name"]),
    }


def _group_key(group: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(group["repo_full_name"]),
        int(group["pr_number"]),
        str(group["job_name"]),
    )


def _resolve_chunk_size(settings: Settings | None) -> int:
    if settings is None:
        return 1000
    return settings.jobs.batch_size


def _resolve_refresh_group_chunk_size(settings: Settings | None) -> int:
    if settings is None:
        return 25
    return max(1, min(settings.jobs.batch_size, settings.jobs.refresh_group_batch_size))


def _resolve_refresh_build_limit(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_REFRESH_BUILD_LIMIT
    return settings.jobs.refresh_build_limit


def _resolve_write_batch_size(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_WRITE_BATCH_SIZE
    return max(1, min(settings.jobs.batch_size, DEFAULT_WRITE_BATCH_SIZE))


def _chunk_count(items: list[Any], chunk_size: int) -> int:
    if not items:
        return 0
    return (len(items) + chunk_size - 1) // chunk_size


def _assign_branch_rows_updated(summary: RefreshBuildDerivedSummary, rows_updated: int) -> None:
    summary.branch_rows_updated += rows_updated


def _assign_case_match_rows_updated(summary: RefreshBuildDerivedSummary, rows_updated: int) -> None:
    summary.case_match_rows_updated += rows_updated


def _assign_failure_category_rows_updated(
    summary: RefreshBuildDerivedSummary,
    rows_updated: int,
) -> None:
    summary.failure_category_rows_updated += rows_updated


def _build_success_watermark(
    watermark: Mapping[str, Any],
    selection_watermarks: Mapping[str, Any],
    impacted_selection: ImpactedBuildSelection,
    *,
    case_run_derived_watermark: int,
) -> dict[str, Any]:
    if impacted_selection.has_more and impacted_selection.build_ids:
        return {
            "last_processed_build_id": impacted_selection.build_ids[-1],
            "last_processed_pr_event_updated_at": _normalize_optional_string(
                watermark.get("last_processed_pr_event_updated_at")
            ),
            "last_processed_case_report_time": _normalize_optional_string(
                watermark.get("last_processed_case_report_time")
            ),
            "pending_refresh": True,
            "pending_target_build_id": _normalize_optional_int(
                selection_watermarks.get("last_processed_build_id")
            ),
            "pending_target_pr_event_updated_at": _normalize_optional_string(
                selection_watermarks.get("last_processed_pr_event_updated_at")
            ),
            "pending_target_case_report_time": _normalize_optional_string(
                selection_watermarks.get("last_processed_case_report_time")
            ),
            CASE_RUN_DERIVED_WATERMARK_KEY: case_run_derived_watermark,
        }

    return {
        "last_processed_build_id": _normalize_optional_int(
            selection_watermarks.get("last_processed_build_id"),
            default=0,
        ),
        "last_processed_pr_event_updated_at": _normalize_optional_string(
            selection_watermarks.get("last_processed_pr_event_updated_at")
        ),
        "last_processed_case_report_time": _normalize_optional_string(
            selection_watermarks.get("last_processed_case_report_time")
        ),
        "pending_refresh": False,
        "pending_target_build_id": None,
        "pending_target_pr_event_updated_at": None,
        "pending_target_case_report_time": None,
        CASE_RUN_DERIVED_WATERMARK_KEY: case_run_derived_watermark,
    }


def _normalize_optional_int(value: Any, *, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    return int(value)


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _refresh_problem_case_run_derived_columns(
    connection: Connection,
    *,
    last_processed_case_run_derived_id: int,
    batch_size: int,
    write_batch_size: int,
) -> int:
    rows = list(
        connection.execute(
            text(
                """
                SELECT id, build_url
                FROM problem_case_runs
                WHERE id > :after_id
                  AND (
                    normalized_build_key IS NULL
                    OR normalized_build_key = ''
                    OR cloud_phase IS NULL
                    OR cloud_phase = ''
                  )
                ORDER BY id
                LIMIT :batch_size
                """
            ),
            {"after_id": last_processed_case_run_derived_id, "batch_size": batch_size},
        ).mappings()
    )
    if not rows:
        max_problem_case_run_id = connection.execute(
            text("SELECT COALESCE(MAX(id), 0) FROM problem_case_runs")
        ).scalar_one()
        return int(max_problem_case_run_id or last_processed_case_run_derived_id)

    payload: list[dict[str, Any]] = []
    next_watermark = last_processed_case_run_derived_id
    for row in rows:
        problem_case_run_id = int(row["id"])
        raw_build_url = _normalize_optional_string(row["build_url"])
        payload.append(
            {
                "id": problem_case_run_id,
                "normalized_build_key": normalize_build_url(raw_build_url),
                "cloud_phase": classify_cloud_phase(raw_build_url),
            }
        )
        next_watermark = problem_case_run_id

    _execute_statement_in_batches(
        connection,
        text(
            """
            UPDATE problem_case_runs
            SET normalized_build_key = :normalized_build_key,
                cloud_phase = :cloud_phase
            WHERE id = :id
            """
        ),
        payload,
        batch_size=write_batch_size,
    )
    LOG.info(
        "problem_case_runs derived columns updated %s rows up to id %s",
        len(payload),
        next_watermark,
    )
    return next_watermark


def _phase_a_branch_enrichment(
    connection: Connection,
    impacted_build_ids: list[int],
    *,
    write_batch_size: int,
) -> int:
    # We intentionally re-read all impacted PR builds in the slice so stale
    # target_branch values can be corrected from the latest snapshot rows.
    pr_build_rows = connection.execute(
        text(
            f"""
            SELECT id, repo_full_name, pr_number, target_branch
            FROM ci_l1_builds
            WHERE is_pr_build = 1
              AND pr_number IS NOT NULL
              AND {_build_in_filter(impacted_build_ids, "build_id")}
            """
        ),
        _build_id_params(impacted_build_ids, "build_id"),
    ).mappings()
    build_rows = list(pr_build_rows)
    if not build_rows:
        return 0

    pr_keys = sorted(
        {
            (str(row["repo_full_name"]), int(row["pr_number"]))
            for row in build_rows
            if row["pr_number"] is not None
        }
    )
    if not pr_keys:
        return 0

    snapshots = {
        (str(row["repo"]), int(row["pr_number"])): str(row["target_branch"])
        for row in _fetch_snapshot_rows(connection, pr_keys)
        if row["target_branch"]
    }

    payload = []
    for row in build_rows:
        branch = snapshots.get((str(row["repo_full_name"]), int(row["pr_number"])))
        if not branch:
            continue
        current_branch = _normalize_optional_string(row["target_branch"])
        if current_branch == branch:
            continue
        payload.append({"id": int(row["id"]), "target_branch": branch})

    if not payload:
        return 0

    _execute_statement_in_batches(
        connection,
        text("UPDATE ci_l1_builds SET target_branch = :target_branch WHERE id = :id"),
        payload,
        batch_size=write_batch_size,
    )
    LOG.info("branch enrichment updated %s rows", len(payload))
    return len(payload)


def _phase_b_flaky_flags_for_groups(
    connection: Connection,
    groups: list[Mapping[str, Any]],
    *,
    write_batch_size: int,
) -> dict[str, Any]:
    if not groups:
        return {
            "groups_recomputed": 0,
            "rows_updated": 0,
            "updated_build_ids": [],
        }

    retest_times_by_pr: dict[tuple[str, int], list] = {}
    if REQUIRE_RETEST_FOR_RETRY_LOOP:
        pr_keys = sorted({(group["repo_full_name"], group["pr_number"]) for group in groups})
        retest_times_by_pr = _fetch_retest_times(connection, pr_keys)

    builds_by_group = _fetch_group_builds_for_groups(connection, groups)
    payload: list[dict[str, int]] = []
    updated_build_ids: set[int] = set()
    for group in groups:
        builds = builds_by_group.get(_group_key(group), [])
        flags = compute_group_flags(
            [
                BuildAttempt(
                    build_id=int(build["id"]),
                    sha=str(build["head_sha"]),
                    state=build["state"],
                    created_at=parse_datetime(build["start_time"]),
                )
                for build in builds
            ],
            require_retest=REQUIRE_RETEST_FOR_RETRY_LOOP,
            retest_times=retest_times_by_pr.get(
                (group["repo_full_name"], group["pr_number"]),
                (),
            ),
        )
        for build in builds:
            build_id = int(build["id"])
            flag = flags[build_id]
            payload.append(
                {
                    "id": build_id,
                    "is_flaky": flag.is_flaky,
                    "is_retry_loop": flag.is_retry_loop,
                }
            )
            updated_build_ids.add(build_id)

    if payload:
        _execute_statement_in_batches(
            connection,
            text(
                """
                UPDATE ci_l1_builds
                SET is_flaky = :is_flaky,
                    is_retry_loop = :is_retry_loop
                WHERE id = :id
                """
            ),
            payload,
            batch_size=write_batch_size,
        )

    LOG.info("flaky flag recomputation updated %s rows across %s groups", len(payload), len(groups))
    return {
        "groups_recomputed": len(groups),
        "rows_updated": len(payload),
        "updated_build_ids": sorted(updated_build_ids),
    }


def _phase_c_case_evidence(
    connection: Connection,
    impacted_build_ids: list[int],
    *,
    write_batch_size: int,
) -> int:
    params = _build_id_params(impacted_build_ids, "build_id")
    matched_rows = connection.execute(_case_match_query(connection, impacted_build_ids), params).mappings()
    matched_build_ids = {int(row["id"]) for row in matched_rows}

    reset_payload = [{"id": build_id, "has_flaky_case_match": 0} for build_id in impacted_build_ids]
    if reset_payload:
        _execute_statement_in_batches(
            connection,
            text(
                """
                UPDATE ci_l1_builds
                SET has_flaky_case_match = :has_flaky_case_match
                WHERE id = :id
                """
            ),
            reset_payload,
            batch_size=write_batch_size,
        )

    match_payload = [
        {"id": build_id, "has_flaky_case_match": 1}
        for build_id in sorted(matched_build_ids)
    ]
    if match_payload:
        _execute_statement_in_batches(
            connection,
            text(
                """
                UPDATE ci_l1_builds
                SET has_flaky_case_match = :has_flaky_case_match
                WHERE id = :id
                """
            ),
            match_payload,
            batch_size=write_batch_size,
        )

    LOG.info("case evidence updated %s matched rows", len(match_payload))
    return len(match_payload)


def _phase_d_failure_category(
    connection: Connection,
    build_ids: list[int],
    *,
    write_batch_size: int,
) -> int:
    if not build_ids:
        return 0

    connection.execute(
        text(
            f"""
            UPDATE ci_l1_builds
            SET failure_category = CASE
                WHEN COALESCE(is_flaky, 0) = 1 OR COALESCE(is_retry_loop, 0) = 1 THEN 'FLAKY_TEST'
                ELSE NULL
            END
            WHERE {_build_in_filter(build_ids, "failure_id")}
            """
        ),
        _build_id_params(build_ids, "failure_id"),
    )

    LOG.info("failure category refreshed for %s rows", len(build_ids))
    return len(build_ids)


def _fetch_new_build_ids(
    connection: Connection,
    *,
    lower_build_id: int,
    upper_build_id: int,
    selection_limit: int,
) -> list[int]:
    if upper_build_id <= lower_build_id:
        return []

    rows = connection.execute(
        text(
            """
            SELECT id
            FROM ci_l1_builds
            WHERE id > :lower_build_id
              AND id <= :upper_build_id
            ORDER BY id
            LIMIT :selection_limit
            """
        ),
        {
            "lower_build_id": lower_build_id,
            "upper_build_id": upper_build_id,
            "selection_limit": selection_limit,
        },
    ).mappings()
    return [int(row["id"]) for row in rows]


def _fetch_pr_event_impacted_build_ids(
    connection: Connection,
    *,
    last_processed_pr_event_updated_at: str | None,
    target_pr_event_updated_at: str | None,
    upper_build_id: int,
    after_build_id: int,
    selection_limit: int,
) -> list[int]:
    if target_pr_event_updated_at is None or target_pr_event_updated_at == last_processed_pr_event_updated_at:
        return []

    params: dict[str, Any] = {
        "target_pr_event_updated_at": target_pr_event_updated_at,
        "upper_build_id": upper_build_id,
        "selection_limit": selection_limit,
    }
    lower_bound_clause = ""
    if last_processed_pr_event_updated_at is not None:
        lower_bound_clause = "AND e.updated_at > :last_processed_pr_event_updated_at"
        params["last_processed_pr_event_updated_at"] = last_processed_pr_event_updated_at

    build_cursor_clause = ""
    if after_build_id > 0:
        build_cursor_clause = "AND b.id > :after_build_id"
        params["after_build_id"] = after_build_id

    rows = connection.execute(
        text(
            f"""
            SELECT DISTINCT b.id
            FROM ci_l1_builds b
            JOIN ci_l1_pr_events e
              ON e.repo = b.repo_full_name
             AND e.pr_number = b.pr_number
            WHERE b.is_pr_build = 1
              AND b.id <= :upper_build_id
              AND e.updated_at <= :target_pr_event_updated_at
              {lower_bound_clause}
              {build_cursor_clause}
            ORDER BY b.id
            LIMIT :selection_limit
            """
        ),
        params,
    ).mappings()
    return [int(row["id"]) for row in rows]


def _fetch_case_impacted_build_ids(
    connection: Connection,
    *,
    last_processed_case_report_time: str | None,
    target_case_report_time: str | None,
    upper_build_id: int,
    after_build_id: int,
    selection_limit: int,
) -> list[int]:
    if target_case_report_time is None or target_case_report_time == last_processed_case_report_time:
        return []

    rows = connection.execute(
        _case_impact_query(
            connection,
            last_processed_case_report_time=last_processed_case_report_time,
            target_case_report_time=target_case_report_time,
            upper_build_id=upper_build_id,
            after_build_id=after_build_id,
            selection_limit=selection_limit,
        ),
        _case_impact_query_params(
            last_processed_case_report_time=last_processed_case_report_time,
            target_case_report_time=target_case_report_time,
            upper_build_id=upper_build_id,
            after_build_id=after_build_id,
            selection_limit=selection_limit,
        ),
    ).mappings()
    return [int(row["id"]) for row in rows]


def _fetch_snapshot_rows(
    connection: Connection,
    pr_keys: list[tuple[str, int]],
):
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (repo, pr_number) in enumerate(pr_keys):
        clauses.append(f"(repo = :repo_{index} AND pr_number = :pr_number_{index})")
        params[f"repo_{index}"] = repo
        params[f"pr_number_{index}"] = pr_number

    return connection.execute(
        text(
            f"""
            SELECT repo, pr_number, target_branch
            FROM ci_l1_pr_events
            WHERE event_type = 'pr_snapshot'
              AND target_branch IS NOT NULL
              AND ({' OR '.join(clauses)})
            """
        ),
        params,
    ).mappings()


def _fetch_impacted_groups(connection: Connection, impacted_build_ids: list[int]):
    return connection.execute(
        text(
            f"""
            SELECT DISTINCT repo_full_name, pr_number, job_name
            FROM ci_l1_builds
            WHERE is_pr_build = 1
              AND pr_number IS NOT NULL
              AND {_build_in_filter(impacted_build_ids, "group_id")}
            ORDER BY repo_full_name, pr_number, job_name
            """
        ),
        _build_id_params(impacted_build_ids, "group_id"),
    ).mappings()


def _fetch_group_builds_for_groups(
    connection: Connection,
    groups: list[Mapping[str, Any]],
) -> dict[tuple[str, int, str], list[Mapping[str, Any]]]:
    if not groups:
        return {}

    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, group in enumerate(groups):
        clauses.append(
            "("
            f"repo_full_name = :repo_full_name_{index} "
            f"AND pr_number = :pr_number_{index} "
            f"AND job_name = :job_name_{index}"
            ")"
        )
        params[f"repo_full_name_{index}"] = group["repo_full_name"]
        params[f"pr_number_{index}"] = group["pr_number"]
        params[f"job_name_{index}"] = group["job_name"]

    rows = connection.execute(
        text(
            f"""
            SELECT id, repo_full_name, pr_number, job_name, state, head_sha, start_time
            FROM ci_l1_builds
            WHERE ({' OR '.join(clauses)})
              AND head_sha IS NOT NULL
              AND head_sha <> ''
            ORDER BY repo_full_name, pr_number, job_name, start_time, id
            """
        ),
        params,
    ).mappings()

    builds_by_group: dict[tuple[str, int, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        builds_by_group.setdefault(_group_key(row), []).append(row)
    return builds_by_group


def _fetch_retest_times(
    connection: Connection,
    pr_keys: list[tuple[str, int]],
) -> dict[tuple[str, int], list]:
    if not pr_keys:
        return {}

    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (repo, pr_number) in enumerate(pr_keys):
        clauses.append(f"(repo = :repo_{index} AND pr_number = :pr_number_{index})")
        params[f"repo_{index}"] = repo
        params[f"pr_number_{index}"] = pr_number

    rows = connection.execute(
        text(
            f"""
            SELECT repo, pr_number, event_time
            FROM ci_l1_pr_events
            WHERE retest_event = 1
              AND ({' OR '.join(clauses)})
            ORDER BY event_time
            """
        ),
        params,
    ).mappings()

    retest_times: dict[tuple[str, int], list] = {}
    for row in rows:
        key = (str(row["repo"]), int(row["pr_number"]))
        event_time = parse_datetime(row["event_time"])
        if event_time is None:
            continue
        retest_times.setdefault(key, []).append(event_time)
    return retest_times


def _case_impact_query(
    connection: Connection,
    *,
    last_processed_case_report_time: str | None,
    target_case_report_time: str,
    upper_build_id: int,
    after_build_id: int,
    selection_limit: int,
) -> Any:
    where_clauses = [
        "p.report_time <= :target_case_report_time",
        "b.id <= :upper_build_id",
    ]
    if last_processed_case_report_time is not None:
        where_clauses.append("p.report_time > :last_processed_case_report_time")
    if after_build_id > 0:
        where_clauses.append("b.id > :after_build_id")
    where_clause = "WHERE " + " AND ".join(where_clauses)

    if connection.dialect.name == "sqlite":
        return text(
            f"""
            SELECT DISTINCT b.id
            FROM ci_l1_builds b
            JOIN problem_case_runs p
              ON p.flaky = 1
             AND p.repo = b.repo_full_name
             AND {_sqlite_normalized_build_url_sql('p.build_url')} = b.normalized_build_url
             AND p.report_time BETWEEN b.start_time AND datetime(b.start_time, '+24 hours')
            {where_clause}
            ORDER BY b.id
            LIMIT :selection_limit
            """
        )

    return text(
        f"""
        SELECT DISTINCT b.id
        FROM ci_l1_builds b
        JOIN problem_case_runs p
          ON p.flaky = 1
         AND p.repo = b.repo_full_name
         AND p.normalized_build_key = b.normalized_build_url
         AND p.report_time BETWEEN b.start_time AND b.start_time + INTERVAL 24 HOUR
        {where_clause}
        ORDER BY b.id
        LIMIT :selection_limit
        """
    )


def _case_impact_query_params(
    *,
    last_processed_case_report_time: str | None,
    target_case_report_time: str,
    upper_build_id: int,
    after_build_id: int,
    selection_limit: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "target_case_report_time": target_case_report_time,
        "upper_build_id": upper_build_id,
        "selection_limit": selection_limit,
    }
    if last_processed_case_report_time is not None:
        params["last_processed_case_report_time"] = last_processed_case_report_time
    if after_build_id > 0:
        params["after_build_id"] = after_build_id
    return params


def _case_match_query(connection: Connection, impacted_build_ids: list[int]) -> Any:
    build_filter = _build_in_filter(impacted_build_ids, "build_id", column_name="b.id")

    if connection.dialect.name == "sqlite":
        return text(
            f"""
            SELECT DISTINCT b.id
            FROM ci_l1_builds b
            JOIN problem_case_runs p
              ON p.flaky = 1
             AND p.repo = b.repo_full_name
             AND {_sqlite_normalized_build_url_sql('p.build_url')} = b.normalized_build_url
             AND p.report_time BETWEEN b.start_time AND datetime(b.start_time, '+24 hours')
            WHERE {build_filter}
            """
        )

    return text(
        f"""
        SELECT DISTINCT b.id
        FROM ci_l1_builds b
        JOIN problem_case_runs p
          ON p.flaky = 1
         AND p.repo = b.repo_full_name
         AND p.normalized_build_key = b.normalized_build_url
         AND p.report_time BETWEEN b.start_time AND b.start_time + INTERVAL 24 HOUR
        WHERE {build_filter}
        """
    )


def _build_in_filter(
    ids: list[int],
    prefix: str,
    *,
    column_name: str = "id",
) -> str:
    return f"{column_name} IN ({', '.join(f':{prefix}_{index}' for index in range(len(ids)))})"


def _build_id_params(ids: list[int], prefix: str) -> dict[str, int]:
    return {f"{prefix}_{index}": build_id for index, build_id in enumerate(ids)}


def _execute_statement_in_batches(
    connection: Connection,
    statement: Any,
    payload: list[Mapping[str, Any]],
    *,
    batch_size: int,
) -> None:
    for payload_chunk in chunked(payload, batch_size):
        connection.execute(statement, payload_chunk)


def _sqlite_normalized_build_url_sql(column_name: str) -> str:
    return f"normalize_build_url({column_name})"


def _mysql_normalized_build_url_sql(column_name: str) -> str:
    trimmed = f"TRIM(COALESCE({column_name}, ''))"
    without_redirect = f"REPLACE({trimmed}, '/display/redirect', '')"
    canonical_host = (
        "CASE "
        f"WHEN {without_redirect} = '' THEN NULL "
        f"WHEN {without_redirect} REGEXP '^https?://prow\\\\.tidb\\\\.net/' THEN 'https://prow.tidb.net' "
        f"WHEN {without_redirect} REGEXP '^https?://do\\\\.pingcap\\\\.net/' THEN 'https://do.pingcap.net' "
        f"WHEN {without_redirect} REGEXP '^https?://jenkins\\\\.jenkins\\\\.svc\\\\.cluster\\\\.local(:[0-9]+)?/' THEN 'https://prow.tidb.net' "
        f"WHEN {without_redirect} NOT REGEXP '^https?://' THEN 'https://prow.tidb.net' "
        "ELSE NULL "
        "END"
    )
    stripped_known_host = (
        "CASE "
        f"WHEN {without_redirect} = '' THEN NULL "
        f"WHEN {without_redirect} REGEXP '^https?://prow\\\\.tidb\\\\.net/' "
        f"THEN REGEXP_REPLACE({without_redirect}, '^https?://prow\\\\.tidb\\\\.net', '') "
        f"WHEN {without_redirect} REGEXP '^https?://do\\\\.pingcap\\\\.net/' "
        f"THEN REGEXP_REPLACE({without_redirect}, '^https?://do\\\\.pingcap\\\\.net', '') "
        f"WHEN {without_redirect} REGEXP '^https?://jenkins\\\\.jenkins\\\\.svc\\\\.cluster\\\\.local(:[0-9]+)?/' "
        f"THEN REGEXP_REPLACE({without_redirect}, '^https?://jenkins\\\\.jenkins\\\\.svc\\\\.cluster\\\\.local(:[0-9]+)?', '') "
        f"ELSE {without_redirect} "
        "END"
    )
    normalized_path = (
        "CASE "
        f"WHEN {stripped_known_host} IS NULL OR {stripped_known_host} = '' THEN NULL "
        f"WHEN LEFT({stripped_known_host}, 1) = '/' THEN {stripped_known_host} "
        f"ELSE CONCAT('/', TRIM(LEADING '/' FROM {stripped_known_host})) "
        "END"
    )
    canonical_path = (
        "CASE "
        f"WHEN {normalized_path} IS NULL THEN NULL "
        f"WHEN {normalized_path} LIKE '/job/%' THEN CONCAT('/jenkins', {normalized_path}) "
        f"ELSE {normalized_path} "
        "END"
    )
    return (
        "CASE "
        f"WHEN {canonical_path} IS NULL OR {canonical_path} = '' OR {canonical_host} IS NULL THEN NULL "
        f"WHEN {canonical_path} LIKE '/jenkins/job/%' OR {canonical_path} LIKE '/view/gs/%' "
        f"THEN CONCAT({canonical_host}, REGEXP_REPLACE({canonical_path}, '/+$', ''), '/') "
        "ELSE NULL "
        "END"
    )
