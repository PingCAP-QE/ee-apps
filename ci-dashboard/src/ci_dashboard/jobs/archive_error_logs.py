from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Any, Mapping

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import ArchiveErrorLogsSummary
from ci_dashboard.jobs.gcs_client import GCSUploader, parse_gcs_uri
from ci_dashboard.jobs.jenkins_client import JenkinsClient

LOG = logging.getLogger(__name__)

FAILURE_LIKE_STATES = ("failure", "error", "timeout", "timed_out", "aborted")

FETCH_CANDIDATE_BUILDS = text(
    """
    SELECT id, url, normalized_build_url, log_gcs_uri, state, build_system,
           start_time, completion_time
    FROM ci_l1_builds
    WHERE build_system = 'JENKINS'
      AND state IN :failure_states
      AND (:build_id IS NULL OR id = :build_id)
      AND (:force = 1 OR log_gcs_uri IS NULL)
    ORDER BY start_time DESC, id DESC
    LIMIT :limit_value
    """
).bindparams(bindparam("failure_states", expanding=True))

UPDATE_ARCHIVE_FIELDS = text(
    """
    UPDATE ci_l1_builds
    SET log_gcs_uri = :log_gcs_uri,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :id
    """
)

TOKEN_VALUE_RE = re.compile(r"(?i)(token|password|passwd|pwd|secret|apikey|api_key)(\s*[=:]\s*)(\S+)")
BEARER_RE = re.compile(r"(?i)(Bearer\s+)(\S+)")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HOME_USER_RE = re.compile(r"(?i)/home/[^/\s]+/")
VAR_LIB_JENKINS_USER_RE = re.compile(r"(?i)/var/lib/jenkins/workspace/[^/\s]+")
TOKEN_QUERY_RE = re.compile(r"(?i)([?&](?:token|access_token|api_key|apikey)=)([^&\s]+)")
INTERNAL_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"
)


def run_archive_error_logs(
    engine: Engine,
    settings: Settings,
    *,
    limit: int | None = None,
    build_id: int | None = None,
    force: bool = False,
    fetcher: JenkinsClient | Any | None = None,
    uploader: GCSUploader | Any | None = None,
) -> ArchiveErrorLogsSummary:
    summary = ArchiveErrorLogsSummary()
    resolved_limit = limit or settings.archive.build_limit

    with engine.begin() as connection:
        candidates = list(
            connection.execute(
                FETCH_CANDIDATE_BUILDS.bindparams(),
                {
                    "failure_states": FAILURE_LIKE_STATES,
                    "build_id": build_id,
                    "force": 1 if force else 0,
                    "limit_value": resolved_limit,
                },
            ).mappings()
        )

    if not candidates:
        return summary

    if settings.archive.gcs_bucket is None:
        raise ValueError("CI_DASHBOARD_GCS_BUCKET is required for archive-error-logs")

    resolved_fetcher = fetcher or JenkinsClient(settings.jenkins)
    resolved_uploader = uploader or GCSUploader()
    owns_fetcher = fetcher is None and hasattr(resolved_fetcher, "close")

    try:
        for build in candidates:
            summary.builds_scanned += 1
            try:
                archived = _archive_single_build(
                    engine,
                    settings,
                    build,
                    force=force,
                    fetcher=resolved_fetcher,
                    uploader=resolved_uploader,
                )
            except Exception:
                summary.builds_failed += 1
                LOG.exception(
                    "failed to archive Jenkins console log",
                    extra={"build_id": build["id"]},
                )
                continue

            if archived:
                summary.builds_archived += 1
            else:
                summary.builds_skipped += 1
    finally:
        if owns_fetcher:
            resolved_fetcher.close()

    return summary


def redact_console_log(text: str) -> str:
    redacted = TOKEN_VALUE_RE.sub(r"\1\2[REDACTED]", text)
    redacted = BEARER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = TOKEN_QUERY_RE.sub(r"\1[REDACTED]", redacted)
    redacted = INTERNAL_IP_RE.sub("[INTERNAL_IP]", redacted)
    redacted = EMAIL_RE.sub("[EMAIL]", redacted)
    redacted = HOME_USER_RE.sub("/home/[USER]/", redacted)
    redacted = VAR_LIB_JENKINS_USER_RE.sub("/var/lib/jenkins/workspace/[JOB]", redacted)
    return redacted


def build_archive_object_ref(
    build: Mapping[str, Any],
    settings: Settings,
    *,
    force: bool,
) -> tuple[str, str]:
    existing = parse_gcs_uri(build.get("log_gcs_uri"))
    if force and existing is not None:
        return existing.bucket, existing.object_name
    archive_folder = resolve_archive_month_folder(build)
    archive_file_name = f"{int(build['id'])}.log"
    object_name = "/".join(
        part
        for part in (settings.archive.gcs_prefix, archive_folder, archive_file_name)
        if part
    )
    return (
        settings.archive.gcs_bucket or "",
        object_name,
    )


def resolve_archive_month_folder(build: Mapping[str, Any]) -> str:
    timestamp = parse_archive_timestamp(build.get("start_time")) or parse_archive_timestamp(
        build.get("completion_time")
    )
    if timestamp is None:
        raise ValueError(f"build {build['id']} is missing both start_time and completion_time")
    return timestamp.strftime("%y%m")


def parse_archive_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported archive timestamp value: {value!r}") from exc


def _archive_single_build(
    engine: Engine,
    settings: Settings,
    build: Mapping[str, Any],
    *,
    force: bool,
    fetcher: Any,
    uploader: Any,
) -> bool:
    if not force and build.get("log_gcs_uri"):
        return False

    build_url = str(build.get("url") or "").strip()
    if not build_url:
        raise ValueError(f"build {build['id']} is missing Jenkins build URL")

    raw_tail = fetcher.fetch_console_tail(
        build_url,
        max_bytes=settings.archive.log_tail_bytes,
    )
    raw_log = _append_failed_node_logs(
        raw_tail,
        build_url=build_url,
        fetcher=fetcher,
    )
    redacted_tail = redact_console_log(raw_log)
    bucket, object_name = build_archive_object_ref(build, settings, force=force)
    log_gcs_uri = uploader.upload_text(
        bucket=bucket,
        object_name=object_name,
        text=redacted_tail,
    )

    with engine.begin() as connection:
        connection.execute(
            UPDATE_ARCHIVE_FIELDS,
            {
                "id": int(build["id"]),
                "log_gcs_uri": log_gcs_uri,
            },
        )
    return True


def _append_failed_node_logs(raw_tail: str, *, build_url: str, fetcher: Any) -> str:
    fetch_failed_node_logs = getattr(fetcher, "fetch_failed_node_logs", None)
    if not callable(fetch_failed_node_logs):
        return raw_tail
    failed_node_logs = fetch_failed_node_logs(build_url)
    if not str(failed_node_logs or "").strip():
        return raw_tail
    return f"{raw_tail.rstrip()}\n{failed_node_logs}"
