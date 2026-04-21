from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import NormalizedBuildRow, SyncBuildsSummary
from ci_dashboard.jobs.build_url_matcher import (
    classify_build_system,
    classify_cloud_phase,
    normalize_build_url,
)
from ci_dashboard.jobs.state_store import (
    get_job_state,
    mark_job_failed,
    mark_job_started,
    mark_job_succeeded,
    save_job_progress,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "ci-sync-builds"

FETCH_SOURCE_ROWS = text(
    """
    SELECT *
    FROM prow_jobs
    WHERE id > :after_id
    ORDER BY id
    LIMIT :batch_size
    """
)


def _build_window_fetch_source_rows_query(has_end: bool):
    end_clause = ""
    if has_end:
        end_clause = "  AND startTime < :start_time_to\n"
    return text(
        f"""
        SELECT *
        FROM prow_jobs
        WHERE id > :after_id
          AND startTime >= :start_time_from
{end_clause}        ORDER BY id
        LIMIT :batch_size
        """
    )


_UPSERT_COLUMNS = (
    "source_prow_row_id",
    "source_prow_job_id",
    "namespace",
    "job_name",
    "job_type",
    "state",
    "optional",
    "report",
    "org",
    "repo",
    "repo_full_name",
    "base_ref",
    "pr_number",
    "is_pr_build",
    "context",
    "url",
    "normalized_build_key",
    "author",
    "retest",
    "event_guid",
    "build_id",
    "pod_name",
    "pending_time",
    "start_time",
    "completion_time",
    "queue_wait_seconds",
    "run_seconds",
    "total_seconds",
    "head_sha",
    "target_branch",
    "cloud_phase",
    "build_system",
    "is_flaky",
    "is_retry_loop",
    "has_flaky_case_match",
    "failure_category",
    "failure_subcategory",
)

def load_watermark(connection: Connection) -> int:
    state = get_job_state(connection, JOB_NAME)
    if state is None:
        return 0
    value = state.watermark.get("last_source_prow_row_id", 0)
    return int(value or 0)


def fetch_source_rows(connection: Connection, after_id: int, batch_size: int) -> list[Mapping[str, Any]]:
    result = connection.execute(
        FETCH_SOURCE_ROWS,
        {"after_id": after_id, "batch_size": batch_size},
    )
    return list(result.mappings())


def fetch_source_rows_for_time_window(
    connection: Connection,
    after_id: int,
    batch_size: int,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
) -> list[Mapping[str, Any]]:
    params: dict[str, Any] = {
        "after_id": after_id,
        "batch_size": batch_size,
        "start_time_from": start_time_from,
    }
    if start_time_to is not None:
        params["start_time_to"] = start_time_to
    result = connection.execute(
        _build_window_fetch_source_rows_query(start_time_to is not None),
        params,
    )
    return list(result.mappings())


def extract_status_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    status = _parse_json_object(_get_first(row, "status"))
    return {
        "pending_time": _parse_datetime(_get_first(status, "pendingTime", "pending_time")),
        "build_id": _coerce_str(_get_first(status, "build_id", "buildId"), allow_none=True),
        "pod_name": _coerce_str(_get_first(status, "pod_name", "podName"), allow_none=True),
        "start_time": _parse_datetime(_get_first(status, "startTime", "start_time")),
        "completion_time": _parse_datetime(_get_first(status, "completionTime", "completion_time")),
    }


def extract_spec_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    spec = _parse_json_object(_get_first(row, "spec"))
    refs = _parse_json_object(spec.get("refs"))
    pulls = refs.get("pulls")
    head_sha = None
    if isinstance(pulls, list) and pulls:
        first_pull = pulls[0]
        if isinstance(first_pull, Mapping):
            head_sha = _coerce_str(_get_first(first_pull, "sha"))
    return {"head_sha": head_sha}


def map_build_row(row: Mapping[str, Any]) -> NormalizedBuildRow:
    status_fields = extract_status_fields(row)
    spec_fields = extract_spec_fields(row)

    source_prow_row_id = _coerce_int(_required(row, "id"))
    source_prow_job_id = _coerce_str(_required(row, "prowJobId", "prow_job_id"))
    namespace = _coerce_str(_required(row, "namespace"))
    job_name = _coerce_str(_required(row, "jobName", "job_name"))
    job_type = _coerce_str(_required(row, "type", "job_type"))
    state = _coerce_str(_required(row, "state"))
    org = _coerce_str(_required(row, "org"))
    repo = _coerce_str(_required(row, "repo"))
    repo_full_name = f"{org}/{repo}"
    pr_number = _coerce_optional_int(_get_first(row, "pull", "pr_number"))
    is_pr_build = pr_number is not None
    url = _coerce_str(_required(row, "url"))
    start_time = _parse_datetime(_get_first(row, "startTime", "start_time")) or status_fields["start_time"]
    if start_time is None:
        raise ValueError(f"prow_jobs row {source_prow_row_id} is missing startTime")
    completion_time = _parse_datetime(_get_first(row, "completionTime", "completion_time")) or status_fields["completion_time"]
    pending_time = status_fields["pending_time"]

    return NormalizedBuildRow(
        source_prow_row_id=source_prow_row_id,
        source_prow_job_id=source_prow_job_id,
        namespace=namespace,
        job_name=job_name,
        job_type=job_type,
        state=state,
        optional=_coerce_bool(_get_first(row, "optional"), default=False),
        report=_coerce_bool(_get_first(row, "report"), default=False),
        org=org,
        repo=repo,
        repo_full_name=repo_full_name,
        base_ref=_coerce_str(_get_first(row, "base_ref"), allow_none=True),
        pr_number=pr_number,
        is_pr_build=is_pr_build,
        context=_coerce_str(_get_first(row, "context"), allow_none=True),
        url=url,
        normalized_build_key=normalize_build_url(url),
        author=_coerce_str(_get_first(row, "author"), allow_none=True),
        retest=_coerce_optional_bool(_get_first(row, "retest")),
        event_guid=_coerce_str(_get_first(row, "event_guid", "eventGuid"), allow_none=True),
        build_id=status_fields["build_id"],
        pod_name=status_fields["pod_name"],
        pending_time=pending_time,
        start_time=start_time,
        completion_time=completion_time,
        queue_wait_seconds=_duration_seconds(start_time, pending_time),
        run_seconds=_duration_seconds(pending_time, completion_time),
        total_seconds=_duration_seconds(start_time, completion_time),
        head_sha=spec_fields["head_sha"],
        target_branch=None,
        cloud_phase=classify_cloud_phase(url),
        build_system=classify_build_system(url),
        is_flaky=False,
        is_retry_loop=False,
        has_flaky_case_match=False,
        failure_category=None,
        failure_subcategory=None,
    )


def upsert_build_batch(connection: Connection, rows: list[NormalizedBuildRow]) -> int:
    if not rows:
        return 0
    payload = [row.as_db_params() for row in rows]
    connection.execute(_build_upsert_statement(connection), payload)
    return len(payload)


def normalize_build_batch(source_rows: list[Mapping[str, Any]]) -> tuple[list[NormalizedBuildRow], int]:
    build_rows: list[NormalizedBuildRow] = []
    skipped_rows = 0
    for row in source_rows:
        try:
            build_rows.append(map_build_row(row))
        except ValueError as exc:
            skipped_rows += 1
            LOG.warning(
                "skipping malformed prow_jobs row",
                extra={
                    "job_name": JOB_NAME,
                    "source_prow_row_id": _get_first(row, "id"),
                    "reason": str(exc),
                },
            )
    return build_rows, skipped_rows


def run_sync_builds(engine: Engine, settings: Settings) -> SyncBuildsSummary:
    summary = SyncBuildsSummary()
    watermark = 0

    with engine.begin() as connection:
        watermark = load_watermark(connection)
        mark_job_started(connection, JOB_NAME, {"last_source_prow_row_id": watermark})

    try:
        while True:
            with engine.begin() as connection:
                source_rows = fetch_source_rows(connection, watermark, settings.jobs.batch_size)
                if not source_rows:
                    break
                build_rows, skipped_rows = normalize_build_batch(source_rows)
                rows_written = upsert_build_batch(connection, build_rows)
                watermark = max(_coerce_int(_required(row, "id")) for row in source_rows)
                save_job_progress(connection, JOB_NAME, {"last_source_prow_row_id": watermark})

                summary.batches_processed += 1
                summary.source_rows_scanned += len(source_rows)
                summary.rows_written += rows_written
                summary.rows_skipped += skipped_rows
                summary.last_source_prow_row_id = watermark

                LOG.info(
                    "processed build batch",
                    extra={
                        "job_name": JOB_NAME,
                        "batch_size": len(source_rows),
                        "rows_written": rows_written,
                        "rows_skipped": skipped_rows,
                        "last_source_prow_row_id": watermark,
                    },
                )

        with engine.begin() as connection:
            mark_job_succeeded(connection, JOB_NAME, {"last_source_prow_row_id": watermark})
        return summary
    except Exception as exc:
        with engine.begin() as connection:
            mark_job_failed(
                connection,
                JOB_NAME,
                {"last_source_prow_row_id": watermark},
                str(exc),
            )
        raise


def run_sync_builds_for_time_window(
    engine: Engine,
    settings: Settings,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
) -> SyncBuildsSummary:
    summary = SyncBuildsSummary()
    after_id = 0

    while True:
        with engine.begin() as connection:
            source_rows = fetch_source_rows_for_time_window(
                connection,
                after_id,
                settings.jobs.batch_size,
                start_time_from=start_time_from,
                start_time_to=start_time_to,
            )
            if not source_rows:
                break

            build_rows, skipped_rows = normalize_build_batch(source_rows)
            rows_written = upsert_build_batch(connection, build_rows)
            after_id = max(_coerce_int(_required(row, "id")) for row in source_rows)

            summary.batches_processed += 1
            summary.source_rows_scanned += len(source_rows)
            summary.rows_written += rows_written
            summary.rows_skipped += skipped_rows
            summary.last_source_prow_row_id = after_id

            LOG.info(
                "processed backfill build batch",
                extra={
                    "job_name": JOB_NAME,
                    "batch_size": len(source_rows),
                    "rows_written": rows_written,
                    "rows_skipped": skipped_rows,
                    "last_source_prow_row_id": after_id,
                    "start_time_from": start_time_from.isoformat(sep=" "),
                    "start_time_to": start_time_to.isoformat(sep=" ") if start_time_to else None,
                },
            )

    return summary


def _required(mapping: Mapping[str, Any], *keys: str) -> Any:
    value = _get_first(mapping, *keys)
    if value is None:
        joined = ", ".join(keys)
        raise ValueError(f"Missing required field: {joined}")
    return value


def _get_first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if not isinstance(key, str):
            continue
        if key in mapping:
            return mapping[key]
    return None


def _parse_json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return {}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"Unsupported JSON object payload: {value!r}")


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return _to_naive_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        raw = raw.replace("Z", "+00:00")
        return _to_naive_utc(datetime.fromisoformat(raw))
    return None


def _to_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _duration_seconds(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    delta = int((end - start).total_seconds())
    if delta < 0:
        return None
    return delta


def _coerce_str(value: Any, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError("Expected string value, got None")
    normalized = str(value)
    if not allow_none and normalized.strip() == "":
        raise ValueError("Expected non-empty string value")
    return normalized


def _coerce_int(value: Any) -> int:
    return int(value)


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _coerce_bool(value: Any, default: bool) -> bool:
    optional = _coerce_optional_bool(value)
    return default if optional is None else optional


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
    raise ValueError(f"Unsupported boolean value: {value!r}")


def _build_upsert_statement(connection: Connection):
    value_list = ", ".join(f":{column}" for column in _UPSERT_COLUMNS)
    if connection.dialect.name == "sqlite":
        assignments = ",\n      ".join(
            f"{column} = excluded.{column}"
            for column in _UPSERT_COLUMNS
            if column not in {"source_prow_job_id", "is_flaky", "is_retry_loop", "has_flaky_case_match", "failure_category", "failure_subcategory"}
        )
        return text(
            f"""
            INSERT INTO ci_l1_builds (
              {", ".join(_UPSERT_COLUMNS)}
            ) VALUES (
              {value_list}
            )
            ON CONFLICT(source_prow_job_id) DO UPDATE SET
              {assignments},
              updated_at = CURRENT_TIMESTAMP
            """
        )
    assignments = ",\n      ".join(
        f"{column} = VALUES({column})"
        for column in _UPSERT_COLUMNS
        if column not in {"source_prow_job_id", "is_flaky", "is_retry_loop", "has_flaky_case_match", "failure_category", "failure_subcategory"}
    )
    return text(
        f"""
        INSERT INTO ci_l1_builds (
          {", ".join(_UPSERT_COLUMNS)}
        ) VALUES (
          {value_list}
        )
        ON DUPLICATE KEY UPDATE
          {assignments},
          updated_at = CURRENT_TIMESTAMP
        """
    )
