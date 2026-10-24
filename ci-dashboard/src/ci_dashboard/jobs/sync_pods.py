from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import SyncPodsSummary
from ci_dashboard.common.sql_helpers import chunked
from ci_dashboard.jobs.build_url_matcher import (
    canonicalize_job_name,
    normalize_build_url,
    normalized_job_path_from_key,
)
from ci_dashboard.jobs.pod_source import cloud_phase_for_source, jenkins_host_for_cloud
from ci_dashboard.jobs.state_store import (
    get_job_state,
    mark_job_failed,
    mark_job_started,
    mark_job_succeeded,
)

JOB_NAME = "ci-sync-pods"
LOGGING_API_URL = "https://logging.googleapis.com/v2/entries:list"
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
KUBERNETES_SERVICE_ACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
KUBERNETES_SERVICE_ACCOUNT_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

POD_EVENT_REASONS = (
    "Scheduled",
    "Pulling",
    "Pulled",
    "Created",
    "Started",
    "Killing",
    "Evicted",
    "NodeLost",
    "Unhealthy",
    "Preempting",
    "FailedScheduling",
    "Failed",
    "BackOff",
    "ErrImagePull",
    "ImagePullBackOff",
)

ABNORMAL_REASON_PRIORITY = (
    "OOMKilled",
    "Evicted",
    "Unhealthy",
    "NodeLost",
    "Preempting",
    "Killing",
)
ABNORMAL_REASON_RANK = {reason: index for index, reason in enumerate(ABNORMAL_REASON_PRIORITY)}
ABNORMAL_EVENT_REASONS = frozenset(ABNORMAL_REASON_PRIORITY[1:])

DEFAULT_POD_EVENT_NAMESPACES = (
    "prow-test-pods",
    "jenkins-tidb",
    "jenkins-tiflow",
)

NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
API_RETRY_ATTEMPTS = 3
API_RETRY_BASE_DELAY_SECONDS = 1.0
POD_NAME_BUILD_NUMBER_MIN_DIGITS = 2
POD_LINK_RECONCILE_WINDOW_HOURS = 72
POD_LINK_RECONCILE_BATCH_SIZE = 1000
JENKINS_POD_NAME_PREFIX_LOOKBACK_DAYS = 30
BUILD_URL_LOOKBACK_DAYS = 30
POD_LINK_RECONCILE_MAX_ROWS = 10_000
BUILD_TIME_GUARD = timedelta(hours=24)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedPodEvent:
    source_project: str
    cluster_name: str | None
    location: str | None
    namespace_name: str | None
    pod_name: str | None
    pod_uid: str | None
    event_reason: str | None
    event_type: str | None
    event_message: str | None
    event_timestamp: datetime
    receive_timestamp: datetime
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    reporting_component: str | None
    reporting_instance: str | None
    source_insert_id: str

    def as_db_params(self) -> dict[str, Any]:
        return {
            "source_project": self.source_project,
            "cluster_name": self.cluster_name,
            "location": self.location,
            "namespace_name": self.namespace_name,
            "pod_name": self.pod_name,
            "pod_uid": self.pod_uid,
            "event_reason": self.event_reason,
            "event_type": self.event_type,
            "event_message": self.event_message,
            "event_timestamp": self.event_timestamp,
            "receive_timestamp": self.receive_timestamp,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "reporting_component": self.reporting_component,
            "reporting_instance": self.reporting_instance,
            "source_insert_id": self.source_insert_id,
        }


@dataclass(frozen=True)
class PodMetadataSnapshot:
    pod_uid: str | None
    labels: dict[str, str]
    annotations: dict[str, str]
    observed_at: datetime | None
    creation_timestamp: datetime | None = None
    abnormal_reason: str | None = None
    abnormal_message: str | None = None
    persistent_volume_names: tuple[str, ...] = ()

    def as_lifecycle_fields(self) -> dict[str, Any]:
        return {
            "pod_labels_json": _json_dumps_or_none(self.labels),
            "pod_annotations_json": _json_dumps_or_none(self.annotations),
            "metadata_observed_at": self.observed_at,
            "pod_created_at": self.creation_timestamp,
            "abnormal_reason": self.abnormal_reason,
            "abnormal_message": self.abnormal_message,
            "pod_author": _pod_label_value(self.labels, "author"),
            "pod_org": _pod_label_value(self.labels, "org"),
            "pod_repo": _pod_label_value(self.labels, "repo"),
            "jenkins_label": _coerce_str(self.labels.get("jenkins/label")),
            "ci_job": _coerce_str(self.annotations.get("ci_job")),
        }


@dataclass(frozen=True)
class JenkinsPodNameBuildRef:
    pod_prefix: str
    build_number: str


def _iter_terminated_container_states(
    container_statuses: Any,
    *,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    if not isinstance(container_statuses, list):
        return []

    terminated_states: list[dict[str, Any]] = []
    for container_status in container_statuses:
        if not isinstance(container_status, dict):
            continue
        for state_key in ("state", "lastState"):
            state = container_status.get(state_key)
            if not isinstance(state, dict):
                continue
            terminated = state.get("terminated")
            if not isinstance(terminated, dict):
                continue
            terminated_at = (
                _parse_datetime(terminated.get("finishedAt"))
                or _parse_datetime(terminated.get("startedAt"))
                or observed_at
            )
            terminated_states.append(
                {
                    "reason": _coerce_str(terminated.get("reason")),
                    "message": _coerce_str(terminated.get("message")),
                    "timestamp": terminated_at,
                }
            )

    return terminated_states


def _select_abnormal_summary(candidates: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    selected_reason: str | None = None
    selected_message: str | None = None
    selected_key: tuple[int, int, int, float] | None = None

    for candidate in candidates:
        reason = _coerce_str(candidate.get("reason"))
        if reason not in ABNORMAL_REASON_RANK:
            continue
        message = _coerce_str(candidate.get("message"))
        source_rank = int(candidate.get("source_rank", 1))
        timestamp = _parse_datetime(candidate.get("timestamp"))
        timestamp_key = -timestamp.timestamp() if isinstance(timestamp, datetime) else float("inf")
        # This table is meant to summarize the most actionable diagnosis, so for the same
        # abnormal reason we prefer a concrete message over an empty higher-authority source.
        key = (
            ABNORMAL_REASON_RANK[reason],
            0 if message else 1,
            source_rank,
            timestamp_key,
        )
        if selected_key is None or key < selected_key:
            selected_reason = reason
            selected_message = message
            selected_key = key

    return selected_reason, selected_message


def _extract_pod_abnormal_summary(
    pod_object: dict[str, Any],
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    status = pod_object.get("status")
    status = status if isinstance(status, dict) else {}
    candidates = [
        {
            "reason": state.get("reason"),
            "message": state.get("message"),
            "timestamp": state.get("timestamp"),
            "source_rank": 0,
        }
        for state in _iter_terminated_container_states(
            status.get("containerStatuses"),
            observed_at=observed_at,
        )
        if _coerce_str(state.get("reason")) == "OOMKilled"
    ]
    status_reason = _coerce_str(status.get("reason"))
    if status_reason in ABNORMAL_EVENT_REASONS:
        candidates.append(
            {
                "reason": status_reason,
                "message": _coerce_str(status.get("message")),
                "timestamp": observed_at,
                "source_rank": 0,
            }
        )

    abnormal_reason, abnormal_message = _select_abnormal_summary(candidates)
    return {
        "abnormal_reason": abnormal_reason,
        "abnormal_message": abnormal_message,
    }


def run_sync_pods(engine: Engine, settings: Settings) -> SyncPodsSummary:
    summary = SyncPodsSummary()
    jenkins_pod_name_url_prefixes: dict[str, str] | None = None
    with engine.begin() as connection:
        watermark = _load_watermark(connection)
        mark_job_started(connection, JOB_NAME, watermark)
        namespaces = _load_target_namespaces(connection)

    now_utc = datetime.now(UTC)
    start_from = _compute_start_from(watermark, now_utc)

    try:
        entries = _fetch_pod_event_entries(
            start_from=start_from,
            end_time=now_utc,
            namespaces=namespaces,
        )
        summary.source_rows_scanned = len(entries)

        normalized_rows = [row for row in (_normalize_logging_entry(item) for item in entries) if row]
        summary.last_receive_timestamp = _max_receive_timestamp(normalized_rows)

        affected_pods: set[tuple[str, str | None, str | None, str | None]] = set()
        for batch in chunked(normalized_rows, settings.jobs.batch_size):
            payload = [row.as_db_params() for row in batch]
            with engine.begin() as connection:
                _upsert_pod_events(connection, payload)
            summary.batches_processed += 1
            summary.event_rows_written += len(batch)
            for row in batch:
                affected_pods.add((row.source_project, row.namespace_name, row.pod_uid, row.pod_name))

        if affected_pods:
            pod_metadata_by_identity = _load_pod_metadata_snapshots(sorted(affected_pods))
            if any(_infer_pod_build_system(namespace_name) == "JENKINS" for _, namespace_name, _, _ in affected_pods):
                jenkins_event_times = [
                    row.event_timestamp
                    for row in normalized_rows
                    if _infer_pod_build_system(row.namespace_name) == "JENKINS"
                ]
                jenkins_reference_time = min(jenkins_event_times) if jenkins_event_times else None
                with engine.begin() as connection:
                    # Learn the prefix mapping once per run instead of rescanning build history per lifecycle batch.
                    jenkins_pod_name_url_prefixes = _load_jenkins_pod_name_url_prefix_map_for_reference(
                        connection,
                        reference_time=jenkins_reference_time,
                    )
            for group in chunked(sorted(affected_pods), settings.jobs.batch_size):
                with engine.begin() as connection:
                    lifecycle_rows = _build_lifecycle_rows(
                        connection,
                        group,
                        pod_metadata_by_identity=pod_metadata_by_identity,
                        jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
                    )
                    if lifecycle_rows:
                        _upsert_pod_lifecycle(connection, lifecycle_rows)
                        _upsert_pvc_pod_mappings(
                            connection,
                            _build_pvc_pod_mapping_rows(
                                lifecycle_rows,
                                pod_metadata_by_identity=pod_metadata_by_identity,
                            ),
                        )
                        summary.lifecycle_rows_upserted += len(lifecycle_rows)
            summary.pods_touched = len(affected_pods)

        with engine.begin() as connection:
            summary.reconciled_rows_updated = _reconcile_recent_lifecycle_rows(
                connection,
                batch_size=settings.jobs.batch_size,
                jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
                reference_time=now_utc,
            )

        new_watermark = {
            "last_receive_timestamp": summary.last_receive_timestamp or watermark.get("last_receive_timestamp"),
        }
        with engine.begin() as connection:
            mark_job_succeeded(connection, JOB_NAME, new_watermark)
        return summary
    except Exception as exc:
        with engine.begin() as connection:
            mark_job_failed(connection, JOB_NAME, watermark, str(exc))
        raise


def run_reconcile_pod_linkage_for_time_window(
    engine: Engine,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None = None,
    batch_size: int | None = None,
    source_project: str | None = None,
    max_rows: int = POD_LINK_RECONCILE_MAX_ROWS,
    after_id: int = 0,
    through_id: int | None = None,
) -> SyncPodsSummary:
    start = _parse_datetime(start_time_from)
    end = (
        _parse_datetime(start_time_to)
        if start_time_to is not None
        else _prefix_reference_window(datetime.now(UTC)) + timedelta(days=1)
    )
    if start is None or end is None or start >= end:
        raise ValueError("Pod reconciliation requires a nonempty [from, to) UTC window")
    size = POD_LINK_RECONCILE_BATCH_SIZE if batch_size is None else batch_size
    if (
        not 1 <= size <= POD_LINK_RECONCILE_BATCH_SIZE
        or not 1 <= max_rows <= POD_LINK_RECONCILE_MAX_ROWS
    ):
        raise ValueError("Pod reconciliation batch size must be 1..1000 and max rows 1..10000")
    if after_id < 0 or (through_id is not None and through_id < after_id):
        raise ValueError("Pod reconciliation requires 0 <= after_id <= through_id")
    if source_project is not None:
        cloud_phase_for_source(source_project)
    summary = SyncPodsSummary(
        reconcile_source_project=source_project,
        reconcile_start_time_from=_format_rfc3339(start),
        reconcile_start_time_to=_format_rfc3339(end),
        reconcile_after_id=after_id,
    )
    with engine.begin() as connection:
        summary.reconcile_through_id = (
            through_id
            if through_id is not None
            else _freeze_reconcile_ceiling(
                connection,
                start_time_from=start,
                start_time_to=end,
                source_project=source_project,
            )
        )
        summary.reconcile_through_id = max(after_id, summary.reconcile_through_id)
        summary.reconcile_missing_time_rows = _count_reconcile_missing_time_rows(
            connection,
            source_project=source_project,
        )
    try:
        while summary.reconciled_rows_scanned < max_rows:
            with engine.begin() as connection:
                rows = _load_lifecycle_rows_for_reconcile(
                    connection,
                    start_time_from=start,
                    start_time_to=end,
                    source_project=source_project,
                    after_lifecycle_id=summary.reconcile_after_id,
                    through_id=summary.reconcile_through_id,
                    limit=min(size, max_rows - summary.reconciled_rows_scanned),
                )
                updated, ambiguous, skipped = _reconcile_lifecycle_batch(connection, rows)
            # Never expose progress until this batch's transaction has committed.
            if not rows:
                summary.reconcile_complete = True
                break
            summary.reconcile_after_id = int(rows[-1]["id"])
            summary.reconciled_rows_scanned += len(rows)
            summary.reconciled_rows_updated += updated
            summary.reconciled_rows_ambiguous += ambiguous
            summary.reconciled_rows_skipped += skipped
            summary.batches_processed += 1
            logger.info("Pod linkage batch committed", extra={"summary": summary.__dict__.copy()})
        if not summary.reconcile_complete:
            with engine.begin() as connection:
                summary.reconcile_complete = not _load_lifecycle_rows_for_reconcile(
                    connection,
                    start_time_from=start,
                    start_time_to=end,
                    source_project=source_project,
                    after_lifecycle_id=summary.reconcile_after_id,
                    through_id=summary.reconcile_through_id,
                    limit=1,
                )
    except Exception:
        logger.exception(
            "Pod linkage reconciliation failed; resume from last committed cursor",
            extra={"summary": summary.__dict__.copy()},
        )
        raise
    return summary


def _load_target_namespaces(connection: Connection) -> list[str]:
    from_env = _read_namespace_override()
    if from_env:
        return from_env
    namespaces: list[str] = []
    seen: set[str] = set()
    for namespace in DEFAULT_POD_EVENT_NAMESPACES:
        namespaces.append(namespace)
        seen.add(namespace)

    statement = text(
        """
        SELECT DISTINCT namespace
        FROM ci_l1_builds
        WHERE namespace IS NOT NULL
        ORDER BY namespace
        """
    )
    rows = connection.execute(statement).mappings()
    for row in rows:
        namespace = str(row["namespace"] or "").strip()
        if namespace == "" or namespace == "apps" or namespace in seen:
            continue
        namespaces.append(namespace)
        seen.add(namespace)
    return namespaces


def _read_namespace_override() -> list[str]:
    raw = (os.environ.get("CI_DASHBOARD_POD_EVENT_NAMESPACES") or "").strip()
    if raw == "":
        return []
    namespaces = [item.strip() for item in raw.split(",")]
    return [item for item in namespaces if item]


def _load_watermark(connection: Connection) -> dict[str, Any]:
    state = get_job_state(connection, JOB_NAME)
    if state is None:
        return {"last_receive_timestamp": None}
    return {"last_receive_timestamp": state.watermark.get("last_receive_timestamp")}


def _compute_start_from(watermark: dict[str, Any], now_utc: datetime) -> datetime:
    overlap_minutes = _read_int_env("CI_DASHBOARD_POD_SYNC_OVERLAP_MINUTES", 15)
    default_lookback_minutes = _read_int_env("CI_DASHBOARD_POD_SYNC_LOOKBACK_MINUTES", 120)
    raw_watermark = watermark.get("last_receive_timestamp")
    if not raw_watermark:
        return now_utc - timedelta(minutes=default_lookback_minutes)
    parsed = _parse_datetime(raw_watermark)
    if parsed is None:
        return now_utc - timedelta(minutes=default_lookback_minutes)
    return parsed - timedelta(minutes=overlap_minutes)


def _read_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _fetch_pod_event_entries(
    *,
    start_from: datetime,
    end_time: datetime,
    namespaces: list[str],
) -> list[dict[str, Any]]:
    project_id = _required_env("CI_DASHBOARD_GCP_PROJECT")
    token = _get_google_access_token()
    filter_parts = [
        'logName="projects/%s/logs/events"' % project_id,
        'resource.type="k8s_pod"',
        'timestamp >= "%s"' % _format_rfc3339(start_from),
        'timestamp < "%s"' % _format_rfc3339(end_time),
        "(" + " OR ".join('jsonPayload.reason="%s"' % reason for reason in POD_EVENT_REASONS) + ")",
    ]
    if namespaces:
        filter_parts.append(
            "(" + " OR ".join('resource.labels.namespace_name="%s"' % _escape_filter(v) for v in namespaces) + ")"
        )

    entries: list[dict[str, Any]] = []
    page_token: str | None = None
    max_pages = _read_int_env("CI_DASHBOARD_POD_SYNC_MAX_PAGES", 200)

    for _ in range(max_pages):
        body = {
            "resourceNames": [f"projects/{project_id}"],
            "filter": " AND ".join(filter_parts),
            "orderBy": "timestamp asc",
            "pageSize": 1000,
        }
        if page_token:
            body["pageToken"] = page_token
        response = _post_json(
            LOGGING_API_URL,
            body,
            headers={"Authorization": f"Bearer {token}"},
        )
        page_entries = response.get("entries")
        if isinstance(page_entries, list):
            entries.extend(item for item in page_entries if isinstance(item, dict))
        page_token = response.get("nextPageToken") or None
        if not page_token:
            break
    return entries


def _escape_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_google_access_token() -> str:
    from_env = (os.environ.get("CI_DASHBOARD_GCP_ACCESS_TOKEN") or "").strip()
    if from_env:
        return from_env

    request = urllib_request.Request(
        METADATA_TOKEN_URL,
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            token = str(payload.get("access_token") or "").strip()
            if token:
                return token
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("Unable to fetch GCP access token from metadata server") from exc
    raise RuntimeError("Metadata server token response missing access_token")


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(API_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))


def _decode_json_object(body: bytes, *, error_context: str) -> dict[str, Any]:
    decoded = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as exc:
        snippet = " ".join(decoded.split())
        if snippet == "":
            snippet = "<empty body>"
        raise RuntimeError(f"{error_context} returned invalid JSON: {snippet[:200]}") from exc
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"{error_context} response is not an object")


def _request_json(
    request: urllib_request.Request,
    *,
    timeout: int,
    error_context: str,
    context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            with urllib_request.urlopen(request, timeout=timeout, context=context) as response:
                return _decode_json_object(response.read(), error_context=error_context)
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if attempt < API_RETRY_ATTEMPTS and exc.code in RETRYABLE_HTTP_STATUS_CODES:
                _sleep_before_retry(attempt)
                continue
            raise RuntimeError(f"{error_context} request failed: HTTP {exc.code}: {body}") from exc
        except urllib_error.URLError as exc:
            if attempt < API_RETRY_ATTEMPTS:
                _sleep_before_retry(attempt)
                continue
            raise RuntimeError(f"{error_context} request failed: {exc.reason}") from exc
    raise AssertionError("unreachable")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib_request.Request(
        url,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ci-dashboard-sync-pods",
            **headers,
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    return _request_json(request, timeout=30, error_context="Logging API")


def _normalize_logging_entry(entry: dict[str, Any]) -> NormalizedPodEvent | None:
    source_project = _extract_project_from_log_name(_coerce_str(entry.get("logName")))
    source_insert_id = _coerce_str(entry.get("insertId"))
    if not source_project or not source_insert_id:
        return None

    resource = entry.get("resource")
    labels = {}
    if isinstance(resource, dict):
        raw_labels = resource.get("labels")
        if isinstance(raw_labels, dict):
            labels = raw_labels

    payload = entry.get("jsonPayload")
    if not isinstance(payload, dict):
        return None

    involved_object = payload.get("involvedObject")
    pod_uid = None
    if isinstance(involved_object, dict):
        pod_uid = _coerce_str(involved_object.get("uid"))

    event_timestamp = _parse_datetime(entry.get("timestamp")) or _parse_datetime(payload.get("lastTimestamp"))
    receive_timestamp = _parse_datetime(entry.get("receiveTimestamp")) or event_timestamp
    if event_timestamp is None or receive_timestamp is None:
        return None

    first_timestamp, last_timestamp = _event_first_last_timestamps(payload)
    return NormalizedPodEvent(
        source_project=source_project,
        cluster_name=_coerce_str(labels.get("cluster_name")),
        location=_coerce_str(labels.get("location")),
        namespace_name=_coerce_str(labels.get("namespace_name")),
        pod_name=_coerce_str(labels.get("pod_name")),
        pod_uid=pod_uid,
        event_reason=_coerce_str(payload.get("reason")),
        event_type=_coerce_str(payload.get("type")),
        event_message=_coerce_str(payload.get("message")),
        event_timestamp=event_timestamp,
        receive_timestamp=receive_timestamp,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        reporting_component=_coerce_str(payload.get("reportingComponent")),
        reporting_instance=_coerce_str(payload.get("reportingInstance")),
        source_insert_id=source_insert_id,
    )


def _event_first_last_timestamps(
    event_object: dict,
) -> tuple[datetime | None, datetime | None]:
    metadata = event_object.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    series = event_object.get("series")
    series = series if isinstance(series, dict) else {}
    first = (
        _parse_datetime(event_object.get("eventTime"))
        or _parse_datetime(event_object.get("firstTimestamp"))
        or _parse_datetime(metadata.get("creationTimestamp"))
    )
    latest = [
        value
        for value in (
            first,
            _parse_datetime(event_object.get("lastTimestamp")),
            _parse_datetime(series.get("lastObservedTime")),
        )
        if value is not None
    ]
    return first, max(latest) if latest else None


def _extract_project_from_log_name(log_name: str | None) -> str | None:
    if not log_name:
        return None
    prefix = "projects/"
    if not log_name.startswith(prefix):
        return None
    parts = log_name.split("/", 2)
    if len(parts) < 2:
        return None
    return parts[1]


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value if text_value else None


def _coerce_str_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _coerce_str(raw_key)
        val = _coerce_str(raw_value)
        if key is None or val is None:
            continue
        output[key] = val
    return output


def _pod_label_value(labels: dict[str, str], name: str) -> str | None:
    return _coerce_str(labels.get(name)) or _coerce_str(labels.get(f"prow.k8s.io/refs.{name}"))


def _json_dumps_or_none(value: dict[str, str]) -> str | None:
    if not value:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    raw = str(value).strip()
    if raw == "":
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _format_rfc3339(value: datetime) -> str:
    aware = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _max_receive_timestamp(rows: Iterable[NormalizedPodEvent]) -> str | None:
    max_ts: datetime | None = None
    for row in rows:
        if max_ts is None or row.receive_timestamp > max_ts:
            max_ts = row.receive_timestamp
    if max_ts is None:
        return None
    return _format_rfc3339(max_ts.replace(tzinfo=UTC))


def _upsert_pod_events(connection: Connection, payload: list[dict[str, Any]]) -> None:
    if not payload:
        return
    statement = _build_pod_events_upsert_statement(connection)
    connection.execute(statement, payload)


def _build_pod_events_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            """
            INSERT INTO ci_l1_pod_events (
              source_project, cluster_name, location, namespace_name,
              pod_name, pod_uid, event_reason, event_type, event_message, event_timestamp, receive_timestamp,
              first_timestamp, last_timestamp, reporting_component, reporting_instance, source_insert_id
            ) VALUES (
              :source_project, :cluster_name, :location, :namespace_name,
              :pod_name, :pod_uid, :event_reason, :event_type, :event_message, :event_timestamp, :receive_timestamp,
              :first_timestamp, :last_timestamp, :reporting_component, :reporting_instance, :source_insert_id
            )
            ON CONFLICT(source_project, source_insert_id) DO UPDATE SET
              cluster_name = excluded.cluster_name,
              location = excluded.location,
              namespace_name = excluded.namespace_name,
              pod_name = excluded.pod_name,
              pod_uid = excluded.pod_uid,
              event_reason = excluded.event_reason,
              event_type = excluded.event_type,
              event_message = excluded.event_message,
              receive_timestamp = excluded.receive_timestamp,
              first_timestamp = excluded.first_timestamp,
              last_timestamp = excluded.last_timestamp,
              reporting_component = excluded.reporting_component,
              reporting_instance = excluded.reporting_instance,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO ci_l1_pod_events (
          source_project, cluster_name, location, namespace_name,
          pod_name, pod_uid, event_reason, event_type, event_message, event_timestamp, receive_timestamp,
          first_timestamp, last_timestamp, reporting_component, reporting_instance, source_insert_id
        ) VALUES (
          :source_project, :cluster_name, :location, :namespace_name,
          :pod_name, :pod_uid, :event_reason, :event_type, :event_message, :event_timestamp, :receive_timestamp,
          :first_timestamp, :last_timestamp, :reporting_component, :reporting_instance, :source_insert_id
        )
        ON DUPLICATE KEY UPDATE
          cluster_name = VALUES(cluster_name),
          location = VALUES(location),
          namespace_name = VALUES(namespace_name),
          pod_name = VALUES(pod_name),
          pod_uid = VALUES(pod_uid),
          event_reason = VALUES(event_reason),
          event_type = VALUES(event_type),
          event_message = VALUES(event_message),
          receive_timestamp = VALUES(receive_timestamp),
          first_timestamp = VALUES(first_timestamp),
          last_timestamp = VALUES(last_timestamp),
          reporting_component = VALUES(reporting_component),
          reporting_instance = VALUES(reporting_instance),
          updated_at = CURRENT_TIMESTAMP
        """
    )


def _build_lifecycle_rows(
    connection: Connection,
    pods: list[tuple[str, str | None, str | None, str | None]],
    *,
    pod_metadata_by_identity: dict[
        tuple[str, str | None, str | None, str | None], PodMetadataSnapshot
    ],
    jenkins_pod_name_url_prefixes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    lifecycle_rows = _load_lifecycle_aggregates(connection, pods)
    if not lifecycle_rows:
        return []

    stored_rows = _load_stored_lifecycle_rows(connection, pods)
    stored_by_identity = {
        (row["source_project"], row["namespace_name"], row["pod_uid"], row["pod_name"]): row
        for row in stored_rows
    }
    stored_metadata = _deserialize_pod_metadata_snapshots_from_lifecycle_rows(stored_rows)
    metadata_by_identity = {**stored_metadata}
    for identity, snapshot in pod_metadata_by_identity.items():
        previous = stored_metadata.get(identity)
        if previous is None or (
            snapshot.observed_at is not None
            and (previous.observed_at is None or snapshot.observed_at >= previous.observed_at)
        ):
            metadata_by_identity[identity] = snapshot
    for lifecycle in lifecycle_rows:
        identity = (
            lifecycle["source_project"],
            lifecycle["namespace_name"],
            lifecycle["pod_uid"],
            lifecycle["pod_name"],
        )
        previous = stored_by_identity.get(identity, {})
        for field in _FIRST_MILESTONE_FIELDS + _LATEST_MILESTONE_FIELDS:
            values = [
                value
                for value in (
                    _parse_datetime(lifecycle.get(field)),
                    _parse_datetime(previous.get(field)),
                )
                if value is not None
            ]
            lifecycle[field] = (
                (min(values) if field in _FIRST_MILESTONE_FIELDS else max(values))
                if values
                else None
            )
        lifecycle["failed_scheduling_count"] = max(
            int(lifecycle.get("failed_scheduling_count") or 0),
            int(previous.get("failed_scheduling_count") or 0),
        )
        for field in _LINKAGE_FIELDS + ("pod_created_at",):
            lifecycle[field] = previous.get(field)
        lifecycle["_stored_linkage_loaded"] = True
    abnormal_event_summaries = _load_abnormal_event_summaries(connection, pods)
    build_metadata_by_identity = _load_build_metadata_map(
        connection,
        lifecycle_rows,
        pod_metadata_by_identity=metadata_by_identity,
        jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
    )
    rows: list[dict[str, Any]] = []
    for lifecycle in lifecycle_rows:
        identity = _pod_identity_from_values(
            source_project=_coerce_str(lifecycle.get("source_project")) or "",
            namespace_name=_coerce_str(lifecycle.get("namespace_name")),
            pod_uid=_coerce_str(lifecycle.get("pod_uid")),
            pod_name=_coerce_str(lifecycle.get("pod_name")),
        )
        namespace_name = _coerce_str(lifecycle.get("namespace_name"))
        pod_metadata = metadata_by_identity.get(identity)
        build_meta = build_metadata_by_identity.get(identity, {})
        merged = {
            **lifecycle,
            **(
                pod_metadata.as_lifecycle_fields() if pod_metadata else _empty_pod_metadata_fields()
            ),
            "build_system": build_meta.get("build_system")
            or _infer_pod_build_system(namespace_name),
            "source_prow_job_id": lifecycle.get("source_prow_job_id")
            or build_meta.get("source_prow_job_id"),
            "normalized_build_url": lifecycle.get("normalized_build_url")
            or build_meta.get("normalized_build_url"),
            "repo_full_name": lifecycle.get("repo_full_name") or build_meta.get("repo_full_name"),
            "job_name": lifecycle.get("job_name")
            or canonicalize_job_name(
                _coerce_str(build_meta.get("job_name")),
                repo_full_name=_coerce_str(build_meta.get("repo_full_name")),
            ),
        }
        if not pod_metadata or pod_metadata.creation_timestamp is None:
            merged["pod_created_at"] = lifecycle.get("pod_created_at")
        previous = stored_by_identity.get(identity, {})
        if identity not in pod_metadata_by_identity:
            for field in ("pod_author", "pod_org", "pod_repo", "jenkins_label", "ci_job"):
                merged[field] = previous.get(field)
        _supplement_jenkins_pod_fields_from_build_metadata(
            merged,
            build_meta,
            build_system=_coerce_str(merged.get("build_system")),
        )
        abnormal_reason, abnormal_message = _select_abnormal_summary(
            [
                {
                    "reason": previous.get("abnormal_reason"),
                    "message": previous.get("abnormal_message"),
                    "source_rank": 0,
                },
                {
                    "reason": _coerce_str(merged.get("abnormal_reason")),
                    "message": _coerce_str(merged.get("abnormal_message")),
                    "source_rank": 0,
                },
                {
                    "reason": _coerce_str(
                        abnormal_event_summaries.get(identity, {}).get("abnormal_reason")
                    ),
                    "message": _coerce_str(
                        abnormal_event_summaries.get(identity, {}).get("abnormal_message")
                    ),
                    "source_rank": 1,
                },
            ]
        )
        merged["abnormal_reason"] = abnormal_reason
        merged["abnormal_message"] = abnormal_message
        schedule_at = _parse_datetime(merged.get("scheduled_at"))
        started_at = _parse_datetime(merged.get("first_started_at"))
        merged["scheduled_at"] = schedule_at
        merged["first_started_at"] = started_at
        if isinstance(schedule_at, datetime) and isinstance(started_at, datetime):
            duration = int((started_at - schedule_at).total_seconds())
            merged["schedule_to_started_seconds"] = duration if duration >= 0 else None
        else:
            merged["schedule_to_started_seconds"] = None
        rows.append(merged)
    return rows


def _supplement_jenkins_pod_fields_from_build_metadata(
    lifecycle_row: dict[str, Any],
    build_meta: dict[str, Any],
    *,
    build_system: str | None,
) -> None:
    if build_system != "JENKINS":
        return

    supplemental_fields = {
        "pod_author": _coerce_str(build_meta.get("author")),
        "pod_org": _coerce_str(build_meta.get("org")),
        "pod_repo": _coerce_str(build_meta.get("repo")),
        "ci_job": _build_ci_job_from_build_metadata(build_meta),
    }
    for field_name, field_value in supplemental_fields.items():
        if _coerce_str(lifecycle_row.get(field_name)) is None and field_value is not None:
            lifecycle_row[field_name] = field_value


def _build_ci_job_from_build_metadata(build_meta: dict[str, Any]) -> str | None:
    repo_full_name = _coerce_str(build_meta.get("repo_full_name"))
    if repo_full_name is None:
        org = _coerce_str(build_meta.get("org"))
        repo = _coerce_str(build_meta.get("repo"))
        if org and repo:
            repo_full_name = f"{org}/{repo}"

    canonicalized = canonicalize_job_name(
        _coerce_str(build_meta.get("job_name")),
        repo_full_name=repo_full_name,
    )
    return canonicalized if canonicalized and "/" in canonicalized else None


_FIRST_MILESTONE_FIELDS = ("scheduled_at", "first_pulling_at", "first_pulled_at", "first_created_at", "first_started_at")
_LATEST_MILESTONE_FIELDS = ("last_failed_scheduling_at", "last_event_at")
_LINKAGE_FIELDS = ("build_system", "source_prow_job_id", "normalized_build_url", "repo_full_name", "job_name", "pod_author", "pod_org", "pod_repo", "ci_job")
_MAX_LIFECYCLE_UNION_PODS = 50


def _load_stored_lifecycle_rows(
    connection: Connection,
    pods: list[tuple[str, str | None, str | None, str | None]],
) -> list[dict[str, Any]]:
    stored = []
    for batch in chunked(pods, _MAX_LIFECYCLE_UNION_PODS):
        branches = []
        params = {}
        for index, identity in enumerate(batch):
            where, branch_params = _build_lifecycle_aggregate_where(index, *identity)
            branches.append(f"SELECT * FROM ci_l1_pod_lifecycle AS events WHERE {where}")
            params.update(branch_params)
        stored.extend(
            dict(row)
            for row in connection.execute(text(" UNION ALL ".join(branches)), params).mappings()
        )
    return stored


_STAGE_TIME_SQL = (
    "COALESCE(events.first_timestamp, "
    "CASE WHEN events.last_timestamp IS NULL THEN events.event_timestamp END)"
)
_LATEST_TIME_SQL = (
    "CASE WHEN events.last_timestamp > events.event_timestamp "
    "THEN events.last_timestamp ELSE events.event_timestamp END"
)
_LIFECYCLE_MILESTONES_SQL = ",\n  ".join([
    *(f"MIN(CASE WHEN events.event_reason = '{reason}' THEN {_STAGE_TIME_SQL} END) AS {column}"
      for reason, column in (
          ("Scheduled", "scheduled_at"), ("Pulling", "first_pulling_at"),
          ("Pulled", "first_pulled_at"), ("Created", "first_created_at"),
          ("Started", "first_started_at"),
      )),
    f"MAX(CASE WHEN events.event_reason = 'FailedScheduling' THEN {_LATEST_TIME_SQL} END) "
    "AS last_failed_scheduling_at",
    "SUM(CASE WHEN events.event_reason = 'FailedScheduling' THEN 1 ELSE 0 END) "
    "AS failed_scheduling_count",
    f"MAX({_LATEST_TIME_SQL}) AS last_event_at",
])
_LIFECYCLE_AGGREGATE_SUFFIX = "  " + _LIFECYCLE_MILESTONES_SQL + """
FROM {events_table}
WHERE {where_clause}
GROUP BY
  events.source_project,
  events.cluster_name,
  events.location,
  events.namespace_name,
  events.pod_name,
  events.pod_uid"""


def _build_lifecycle_aggregate_where(
    index: int,
    source_project: str,
    namespace_name: str | None,
    pod_uid: str | None,
    pod_name: str | None,
) -> tuple[str, dict[str, Any]]:
    conditions: list[str] = [f"events.source_project = :sp_{index}"]
    params: dict[str, Any] = {f"sp_{index}": source_project}
    for col, val in [
        ("namespace_name", namespace_name),
        ("pod_uid", pod_uid),
        ("pod_name", pod_name),
    ]:
        if val is None:
            conditions.append(f"events.{col} IS NULL")
        else:
            param = f"{col}_{index}"
            conditions.append(f"events.{col} = :{param}")
            params[param] = val
    return "\n      AND ".join(conditions), params


def _load_lifecycle_aggregates(
    connection: Connection,
    pods: list[tuple[str, str | None, str | None, str | None]],
) -> list[dict[str, Any]]:
    if not pods:
        return []
    if len(pods) == 1:
        return _load_single_lifecycle_aggregate(connection, pods[0])

    all_rows: list[dict[str, Any]] = []
    events_table = _pod_events_table_expr(connection.dialect.name)
    for offset in range(0, len(pods), _MAX_LIFECYCLE_UNION_PODS):
        sub_batch = pods[offset : offset + _MAX_LIFECYCLE_UNION_PODS]
        branches: list[str] = []
        params: dict[str, Any] = {}
        for index, (source_project, namespace_name, pod_uid, pod_name) in enumerate(sub_batch):
            where_clause, branch_params = _build_lifecycle_aggregate_where(
                index, source_project, namespace_name, pod_uid, pod_name
            )
            params.update(branch_params)
            branches.append(
                f"""\
SELECT
  events.source_project,
  events.cluster_name,
  events.location,
  events.namespace_name,
  events.pod_name,
  events.pod_uid,
{_LIFECYCLE_AGGREGATE_SUFFIX.format(events_table=events_table, where_clause=where_clause)}"""
            )
        sql = "\nUNION ALL\n".join(branches)
        rows = connection.execute(text(sql), params).mappings()
        all_rows.extend(dict(row) for row in rows)
    return all_rows


def _load_single_lifecycle_aggregate(
    connection: Connection,
    pod: tuple[str, str | None, str | None, str | None],
) -> list[dict[str, Any]]:
    source_project, namespace_name, pod_uid, pod_name = pod
    events_table = _pod_events_table_expr(connection.dialect.name)
    statement = text(
        f"""
        SELECT
          events.source_project,
          events.cluster_name,
          events.location,
          events.namespace_name,
          events.pod_name,
          events.pod_uid,
          {_LIFECYCLE_MILESTONES_SQL}
        FROM {events_table}
        WHERE events.source_project = :source_project
          AND {_null_safe_equals_sql('events.namespace_name', ':namespace_name', connection.dialect.name)}
          AND {_null_safe_equals_sql('events.pod_uid', ':pod_uid', connection.dialect.name)}
          AND {_null_safe_equals_sql('events.pod_name', ':pod_name', connection.dialect.name)}
        GROUP BY
          events.source_project,
          events.cluster_name,
          events.location,
          events.namespace_name,
          events.pod_name,
          events.pod_uid
        """
    )
    rows = connection.execute(
        statement,
        {
            "source_project": source_project,
            "namespace_name": namespace_name,
            "pod_uid": pod_uid,
            "pod_name": pod_name,
        },
    ).mappings()
    return [dict(row) for row in rows]


def _load_abnormal_event_summaries(
    connection: Connection,
    pods: list[tuple[str, str | None, str | None, str | None]],
) -> dict[tuple[str, str | None, str | None, str | None], dict[str, Any]]:
    if not pods:
        return {}

    candidates_by_identity: dict[tuple[str, str | None, str | None, str | None], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    events_table = _pod_events_table_expr(connection.dialect.name)
    for offset in range(0, len(pods), _MAX_LIFECYCLE_UNION_PODS):
        sub_batch = pods[offset : offset + _MAX_LIFECYCLE_UNION_PODS]
        branches: list[str] = []
        params: dict[str, Any] = {}
        for index, (source_project, namespace_name, pod_uid, pod_name) in enumerate(sub_batch):
            where_clause, branch_params = _build_lifecycle_aggregate_where(
                index,
                source_project,
                namespace_name,
                pod_uid,
                pod_name,
            )
            reason_placeholders: list[str] = []
            for reason_index, reason in enumerate(sorted(ABNORMAL_EVENT_REASONS)):
                param_name = f"abnormal_reason_{index}_{reason_index}"
                reason_placeholders.append(f":{param_name}")
                branch_params[param_name] = reason
            branches.append(
                f"""
                SELECT
                  events.source_project,
                  events.namespace_name,
                  events.pod_uid,
                  events.pod_name,
                  events.event_reason,
                  events.event_message,
                  {_LATEST_TIME_SQL} AS event_timestamp
                FROM {events_table}
                WHERE {where_clause}
                  AND events.event_reason IN ({", ".join(reason_placeholders)})
                """
            )
            params.update(branch_params)

        rows = connection.execute(
            text("\nUNION ALL\n".join(branches)),
            params,
        ).mappings()
        for row in rows:
            identity = _pod_identity_from_values(
                source_project=_coerce_str(row.get("source_project")) or "",
                namespace_name=_coerce_str(row.get("namespace_name")),
                pod_uid=_coerce_str(row.get("pod_uid")),
                pod_name=_coerce_str(row.get("pod_name")),
            )
            candidates_by_identity[identity].append(
                {
                    "reason": _coerce_str(row.get("event_reason")),
                    "message": _coerce_str(row.get("event_message")),
                    "timestamp": row.get("event_timestamp"),
                    "source_rank": 1,
                }
            )

    summaries: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = {}
    for identity, candidates in candidates_by_identity.items():
        abnormal_reason, abnormal_message = _select_abnormal_summary(candidates)
        if abnormal_reason is None:
            continue
        summaries[identity] = {
            "abnormal_reason": abnormal_reason,
            "abnormal_message": abnormal_message,
        }
    return summaries


def _pod_events_table_expr(dialect_name: str) -> str:
    table = "ci_l1_pod_events AS events"
    if dialect_name != "sqlite":
        table = "ci_l1_pod_events events USE INDEX(idx_ci_l1_pod_events_identity_time)"
    return table


def _null_safe_equals_sql(left: str, right: str, dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return f"{left} IS {right}"
    return f"{left} <=> {right}"


def _datetime_lower_bound_sql(column: str, parameter: str, dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return f"datetime({column}) >= datetime({parameter})"
    return f"{column} >= {parameter}"


def _datetime_upper_bound_sql(column: str, parameter: str, dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return f"datetime({column}) < datetime({parameter})"
    return f"{column} < {parameter}"


def _load_build_metadata_map(
    connection: Connection,
    lifecycle_rows: list[dict[str, Any]],
    *,
    pod_metadata_by_identity: dict[
        tuple[str, str | None, str | None, str | None], PodMetadataSnapshot
    ],
    jenkins_pod_name_url_prefixes: dict[str, str] | None = None,
) -> dict[tuple[str, str | None, str | None, str | None], dict[str, Any]]:
    results: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = {}
    scoped_rows: dict[str, list[tuple[tuple, dict[str, Any]]]] = defaultdict(list)
    prefix_maps: dict[tuple[str, datetime], dict[str, str]] = {}
    # Metadata-only watcher rows are freshly constructed. Check saved links too;
    # the DB upsert independently repeats the guard for races after this read.
    stored = _load_stored_lifecycle_rows(
        connection,
        [
            (
                row.get("source_project"),
                row.get("namespace_name"),
                row.get("pod_uid"),
                row.get("pod_name"),
            )
            for row in lifecycle_rows
            if not row.get("_stored_linkage_loaded") and "id" not in row
        ],
    )
    stored_by_identity = {
        (row["source_project"], row["namespace_name"], row["pod_uid"], row["pod_name"]): row
        for row in stored
    }
    for incoming_row in lifecycle_rows:
        identity = (
            incoming_row.get("source_project"),
            incoming_row.get("namespace_name"),
            incoming_row.get("pod_uid"),
            incoming_row.get("pod_name"),
        )
        previous = stored_by_identity.get(identity, {})
        row = {**incoming_row}
        for field in _LINKAGE_FIELDS + ("scheduled_at", "pod_created_at"):
            if previous.get(field) is not None:
                row[field] = previous[field]
        try:
            cloud = cloud_phase_for_source(str(row.get("source_project") or ""))
        except ValueError:
            results[identity] = {"attribution_reason": "invalid_source"}
            logger.warning(
                "Pod attribution rejected",
                extra={"reason": "invalid_source", "lifecycle_id": row.get("id")},
            )
            continue
        metadata = pod_metadata_by_identity.get(identity)
        reference = (
            _parse_datetime(row.get("scheduled_at"))
            or _parse_datetime(row.get("pod_created_at"))
            or (metadata.creation_timestamp if metadata else None)
        )
        system = _infer_pod_build_system(_coerce_str(row.get("namespace_name")))
        if row.get("build_system") not in {None, "", "UNKNOWN", system}:
            results[identity] = {"attribution_reason": "contradictory_existing_build_system"}
            continue
        payload = {**row, "reference_time": _parse_datetime(reference), "build_system": system}
        existing_url = _coerce_str(row.get("normalized_build_url"))
        if existing_url and not _url_in_scope(existing_url, cloud, system):
            results[identity] = {"attribution_reason": "contradictory_existing_url"}
            continue
        if system == "JENKINS":
            metadata_url, reason = _jenkins_metadata_evidence(metadata, cloud_phase=cloud)
            if reason:
                results[identity] = {"attribution_reason": reason}
                continue
            payload["metadata_url"] = metadata_url
            label_url = (
                _build_jenkins_url_from_label_and_ci_job(
                    org=_coerce_str(metadata.labels.get("org")),
                    repo=_coerce_str(metadata.labels.get("repo")),
                    ci_job=_coerce_str(metadata.annotations.get("ci_job")),
                    jenkins_label=_coerce_str(metadata.labels.get("jenkins/label")),
                    cloud_phase=cloud,
                )
                if metadata
                else None
            )
            candidate = metadata_url or label_url
            name_url = None
            # Weaker evidence cannot rescue contradictory metadata. If it matches
            # a different build identity, it is a contradiction rather than a winner.
            if reference is not None and _parse_jenkins_pod_name_build_ref(
                _coerce_str(row.get("pod_name"))
            ):
                window = _prefix_reference_window(reference)
                key = (cloud, window)
                if key not in prefix_maps:
                    supplied = jenkins_pod_name_url_prefixes
                    if isinstance(supplied, _JenkinsPrefixMap) and supplied.scope == key:
                        prefix_maps[key] = supplied
                    else:
                        prefix_maps[key] = _load_jenkins_pod_name_url_prefix_map(
                            connection,
                            cloud_phase=cloud,
                            reference_time=window,
                        )
                prefix_map = prefix_maps[key]
                pod_ref = _parse_jenkins_pod_name_build_ref(_coerce_str(row.get("pod_name")))
                if (
                    isinstance(prefix_map, _JenkinsPrefixMap)
                    and pod_ref
                    and pod_ref.pod_prefix in prefix_map.ambiguous_prefixes
                ):
                    if candidate is None and existing_url is None:
                        results[identity] = {"attribution_reason": "ambiguous_prefix"}
                        continue
                else:
                    name_url = _extract_normalized_build_url_from_pod_name(
                        _coerce_str(row.get("pod_name")),
                        prefix_map,
                    )
            if any(url and not _url_in_scope(url, cloud, system) for url in (candidate, name_url)):
                results[identity] = {"attribution_reason": "contradictory_prefix_url"}
                continue
            if existing_url and candidate and existing_url != candidate:
                results[identity] = {"attribution_reason": "conflicting_candidate_urls"}
                continue
            payload["primary_url"] = candidate or existing_url
            payload["candidate_urls"] = list(
                dict.fromkeys(
                    url for url in (metadata_url, label_url, name_url, existing_url) if url
                )
            )
        elif system == "PROW_NATIVE":
            label_id = _coerce_str(metadata.labels.get("prow.k8s.io/id")) if metadata else None
            existing_id = _coerce_str(row.get("source_prow_job_id"))
            if label_id and existing_id and label_id != existing_id:
                results[identity] = {"attribution_reason": "conflicting_prow_ids"}
                continue
            payload["exact_prow_id"] = label_id or existing_id
        else:
            continue
        scoped_rows[cloud].append((identity, payload))

    for cloud, entries in scoped_rows.items():
        urls = sorted({url for _, row in entries for url in row.get("candidate_urls", [])})
        references = [row["reference_time"] for _, row in entries if row.get("reference_time")]
        candidates = (
            _load_build_candidates_by_normalized_url(
                connection,
                normalized_build_urls=urls,
                cloud_phase=cloud,
                start_time_cutoff=min(references) - BUILD_TIME_GUARD if references else None,
                start_time_ceiling=max(references) + BUILD_TIME_GUARD if references else None,
            )
            if references
            else {}
        )
        prow_rows = [row for _, row in entries if row["build_system"] == "PROW_NATIVE"]
        direct = (
            _load_direct_build_metadata_map(
                connection,
                pod_names=sorted(
                    {str(row["pod_name"]) for row in prow_rows if row.get("pod_name")}
                ),
                cloud_phase=cloud,
                source_prow_job_ids=sorted(
                    {row["exact_prow_id"] for row in prow_rows if row.get("exact_prow_id")}
                ),
                start_time_from=min(references) - BUILD_TIME_GUARD if references else None,
                start_time_to=max(references) + BUILD_TIME_GUARD if references else None,
            )
            if prow_rows
            else []
        )
        key_conflicts = {
            "JENKINS": _load_build_url_key_conflicts(
                connection, urls, cloud_phase=cloud, build_system="JENKINS"
            ),
            "PROW_NATIVE": _load_build_url_key_conflicts(
                connection,
                sorted({build["normalized_build_url"] for build in direct}),
                cloud_phase=cloud,
                build_system="PROW_NATIVE",
            ),
        }
        for identity, row in entries:
            if row["build_system"] == "JENKINS":
                resolved = _resolve_jenkins_build_metadata(
                    scheduled_at=row["reference_time"],
                    candidate_urls=row.get("candidate_urls", []),
                    build_candidates_by_url=candidates,
                    retain_unmatched_url=bool(row.get("metadata_url")),
                    primary_url=row.get("primary_url"),
                )
            else:
                exact_id = row.get("exact_prow_id")
                matches = [
                    build
                    for build in direct
                    if (
                        build.get("source_prow_job_id") == exact_id
                        if exact_id
                        else build.get("pod_name") == row.get("pod_name")
                        and _within_build_time_guard(build, row["reference_time"])
                    )
                ]
                resolved = _unique_build_metadata(matches, build_system="PROW_NATIVE")
            collision = key_conflicts[row["build_system"]].get(resolved.get("normalized_build_url"))
            if collision:
                resolved = {"attribution_reason": collision}
            if (
                not resolved.get("_matched_build")
                and not resolved.get("attribution_reason")
                and row.get("source_prow_job_id")
                and resolved.get("normalized_build_url") != row.get("normalized_build_url")
            ):
                resolved = {"attribution_reason": "unverified_existing_link"}
            if resolved.get("_matched_build") and any(
                _coerce_str(row.get(field))
                and _coerce_str(row.get(field)) != _coerce_str(resolved.get(field))
                for field in ("source_prow_job_id", "normalized_build_url")
            ):
                resolved = {"attribution_reason": "contradictory_existing_link"}
            if resolved:
                results[identity] = resolved
    for identity, result in results.items():
        if result.get("attribution_reason"):
            logger.info(
                "Pod attribution unresolved",
                extra={
                    "reason": result["attribution_reason"],
                    "source_project": identity[0],
                    "pod_uid": identity[2],
                },
            )
    return results


def _load_build_url_key_conflicts(
    connection: Connection,
    urls: list[str],
    *,
    cloud_phase: str,
    build_system: str,
) -> dict[str, str]:
    if not urls:
        return {}
    params = {f"url_{index}": url for index, url in enumerate(urls)}
    placeholders = ", ".join(f":{name}" for name in params)
    # Runtime Insights joins on URL alone. This is a rejection-only audit,
    # deliberately independent of time/scope, never an attribution fallback.
    rows = connection.execute(
        text(
            "SELECT normalized_build_url, cloud_phase, build_system FROM ci_l1_builds "
            f"WHERE normalized_build_url IN ({placeholders})"
        ),
        params,
    ).mappings()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["normalized_build_url"]].append(row)
    conflicts = {}
    for url, builds in grouped.items():
        if any(
            build["cloud_phase"] != cloud_phase or build["build_system"] != build_system
            for build in builds
        ):
            conflicts[url] = "cross_scope_url_collision"
        elif len(builds) > 1:
            conflicts[url] = "ambiguous_build_key"
    return conflicts


def _url_in_scope(url: str, cloud_phase: str, build_system: str) -> bool:
    prefix = "/jenkins/job/" if build_system == "JENKINS" else "/view/gs/"
    return url.startswith(jenkins_host_for_cloud(cloud_phase) + prefix)


def _within_build_time_guard(build: dict[str, Any], reference: datetime | None) -> bool:
    start = _parse_datetime(build.get("start_time"))
    return (
        start is not None and reference is not None and abs(start - reference) <= BUILD_TIME_GUARD
    )


def _unique_build_metadata(matches: list[dict[str, Any]], *, build_system: str) -> dict[str, Any]:
    identities = {
        (row.get("id"), row.get("source_prow_job_id"), row.get("normalized_build_url"))
        for row in matches
    }
    if len(identities) > 1:
        return {"attribution_reason": "ambiguous_build_candidates"}
    if not matches:
        return {}
    return {**matches[0], "build_system": build_system, "_matched_build": True}


def _load_direct_build_metadata_map(
    connection: Connection,
    *,
    pod_names: list[str],
    cloud_phase: str = "GCP",
    source_prow_job_ids: list[str] | None = None,
    start_time_from: datetime | None = None,
    start_time_to: datetime | None = None,
) -> list[dict[str, Any]]:
    if not pod_names and not source_prow_job_ids:
        return []

    placeholders: list[str] = []
    params: dict[str, Any] = {
        "cloud_phase": cloud_phase,
        "controller_prefix": jenkins_host_for_cloud(cloud_phase) + "/view/gs/%",
    }
    for index, pod_name in enumerate(pod_names):
        param_name = f"pod_name_{index}"
        placeholders.append(f":{param_name}")
        params[param_name] = pod_name

    conditions = []
    if placeholders and start_time_from is not None and start_time_to is not None:
        lower = _datetime_lower_bound_sql("start_time", ":start_time_from", connection.dialect.name)
        upper = (
            "julianday(start_time) <= julianday(:start_time_to)"
            if connection.dialect.name == "sqlite"
            else "start_time <= :start_time_to"
        )
        conditions.append(f"(pod_name IN ({', '.join(placeholders)}) AND {lower} AND {upper})")
        params.update(start_time_from=start_time_from, start_time_to=start_time_to)
    id_placeholders = []
    for index, prow_id in enumerate(source_prow_job_ids or []):
        param = f"prow_id_{index}"
        params[param] = prow_id
        id_placeholders.append(f":{param}")
    if id_placeholders:
        conditions.append(f"source_prow_job_id IN ({', '.join(id_placeholders)})")
    if not conditions:
        return []
    rows = connection.execute(
        text(
            f"""
            SELECT
              id,
              pod_name,
              source_prow_job_id,
              normalized_build_url,
              org,
              repo,
              repo_full_name,
              job_name,
              author,
              build_system,
              start_time
            FROM ci_l1_builds
            WHERE ({" OR ".join(conditions)})
              AND cloud_phase = :cloud_phase
              AND build_system = 'PROW_NATIVE'
              AND normalized_build_url LIKE :controller_prefix
            """
        ),
        params,
    ).mappings()

    return [
        dict(row)
        for row in rows
        if _url_in_scope(str(row["normalized_build_url"]), cloud_phase, "PROW_NATIVE")
    ]


def _load_build_candidates_by_normalized_url(
    connection: Connection,
    *,
    normalized_build_urls: list[str],
    start_time_cutoff: datetime | None = None,
    start_time_ceiling: datetime | None = None,
    cloud_phase: str = "GCP",
) -> dict[str, list[dict[str, Any]]]:
    if not normalized_build_urls:
        return {}

    if start_time_cutoff is None:
        lookback_days = _read_int_env(
            "CI_DASHBOARD_BUILD_URL_LOOKBACK_DAYS",
            BUILD_URL_LOOKBACK_DAYS,
        )
        start_time_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=lookback_days)

    params: dict[str, Any] = {
        "start_time_cutoff": start_time_cutoff,
        "cloud_phase": cloud_phase,
        "controller_prefix": jenkins_host_for_cloud(cloud_phase) + "/jenkins/job/%",
    }
    placeholders: list[str] = []
    for index, normalized_build_url in enumerate(normalized_build_urls):
        param_name = f"normalized_build_url_{index}"
        placeholders.append(f":{param_name}")
        params[param_name] = normalized_build_url

    start_time_clause = _datetime_lower_bound_sql(
        "start_time",
        ":start_time_cutoff",
        connection.dialect.name,
    )
    ceiling_clause = ""
    if start_time_ceiling is not None:
        params["start_time_ceiling"] = start_time_ceiling
        ceiling_clause = (
            "AND julianday(start_time) <= julianday(:start_time_ceiling)"
            if connection.dialect.name == "sqlite"
            else "AND start_time <= :start_time_ceiling"
        )
    rows = connection.execute(
        text(
            f"""
            SELECT
              id,
              source_prow_job_id,
              normalized_build_url,
              org,
              repo,
              repo_full_name,
              job_name,
              author,
              start_time
            FROM ci_l1_builds
            WHERE normalized_build_url IS NOT NULL
              AND normalized_build_url IN ({", ".join(placeholders)})
              AND cloud_phase = :cloud_phase
              AND build_system = 'JENKINS'
              AND normalized_build_url LIKE :controller_prefix
              AND {start_time_clause}
              {ceiling_clause}
            ORDER BY start_time DESC
            """
        ),
        params,
    ).mappings()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        normalized_build_url = _coerce_str(row.get("normalized_build_url"))
        if normalized_build_url is None or not _url_in_scope(
            normalized_build_url, cloud_phase, "JENKINS"
        ):
            continue
        grouped[normalized_build_url].append(dict(row))
    return dict(grouped)


def _prefix_reference_window(reference_time: datetime) -> datetime:
    reference = _parse_datetime(reference_time)
    assert reference is not None
    return reference.replace(hour=0, minute=0, second=0, microsecond=0)


class _JenkinsPrefixMap(dict[str, str]):
    """A discovery result is reusable only for its controller and UTC-day window."""

    def __init__(
        self,
        values: dict[str, str],
        *,
        cloud_phase: str,
        reference_time: datetime,
        ambiguous_prefixes: set[str] | None = None,
    ):
        super().__init__(values)
        self.scope = (cloud_phase, _prefix_reference_window(reference_time))
        self.ambiguous_prefixes = ambiguous_prefixes or set()


def _load_jenkins_pod_name_url_prefix_map_for_reference(
    connection: Connection,
    *,
    reference_time: datetime | None,
    cloud_phase: str = "GCP",
) -> dict[str, str]:
    return _load_jenkins_pod_name_url_prefix_map(
        connection,
        cloud_phase=cloud_phase,
        reference_time=reference_time,
    )


def _load_jenkins_pod_name_url_prefix_map(
    connection: Connection,
    *,
    cloud_phase: str = "GCP",
    reference_time: datetime | None = None,
) -> dict[str, str]:
    lookback_days = _read_int_env(
        "CI_DASHBOARD_JENKINS_POD_NAME_PREFIX_LOOKBACK_DAYS",
        JENKINS_POD_NAME_PREFIX_LOOKBACK_DAYS,
    )
    resolved_reference_time = _prefix_reference_window(reference_time or datetime.now(UTC))
    cutoff = resolved_reference_time - timedelta(days=lookback_days)
    window_end = resolved_reference_time + timedelta(days=1)
    controller_prefix = jenkins_host_for_cloud(cloud_phase) + "/jenkins/job/%"
    start_time_clause = _datetime_lower_bound_sql("start_time", ":cutoff", connection.dialect.name)
    end_time_clause = _datetime_upper_bound_sql(
        "start_time", ":window_end", connection.dialect.name
    )
    rows = connection.execute(
        text(
            f"""
            WITH latest_job_samples AS (
              SELECT
                job_name,
                MAX(start_time) AS latest_start_time
              FROM ci_l1_builds
              WHERE build_system = 'JENKINS'
                AND cloud_phase = :cloud_phase
                AND normalized_build_url LIKE :controller_prefix
                AND {end_time_clause}
                AND job_name IS NOT NULL
                AND pod_name IS NOT NULL
                AND normalized_build_url IS NOT NULL
                AND {start_time_clause}
              GROUP BY job_name
            )
            SELECT
              b.pod_name,
              b.normalized_build_url,
              b.start_time
            FROM ci_l1_builds b
            JOIN latest_job_samples latest
              ON latest.job_name = b.job_name
             AND latest.latest_start_time = b.start_time
            WHERE b.build_system = 'JENKINS'
              AND b.cloud_phase = :cloud_phase
              AND b.normalized_build_url LIKE :controller_prefix
              AND b.pod_name IS NOT NULL
              AND b.normalized_build_url IS NOT NULL
            ORDER BY b.start_time DESC, b.id DESC
            """
        ),
        {
            "cutoff": cutoff,
            "window_end": window_end,
            "cloud_phase": cloud_phase,
            "controller_prefix": controller_prefix,
        },
    ).mappings()

    candidates_by_prefix: dict[str, dict[str, tuple[int, datetime | None]]] = defaultdict(dict)
    for row in rows:
        pod_name = _coerce_str(row.get("pod_name"))
        normalized_build_url = _normalize_jenkins_runtime_url(
            _coerce_str(row.get("normalized_build_url")), cloud_phase=cloud_phase
        )
        if pod_name is None or normalized_build_url is None:
            continue
        parsed_ref = _parse_jenkins_pod_name_build_ref(pod_name)
        if parsed_ref is None:
            continue
        if not normalized_build_url.endswith(f"/{parsed_ref.build_number}/"):
            continue
        normalized_job_url = normalized_job_path_from_key(normalized_build_url)
        if normalized_job_url is None:
            continue
        current_count, current_latest = candidates_by_prefix[parsed_ref.pod_prefix].get(
            normalized_job_url, (0, None)
        )
        start_time = _parse_datetime(row.get("start_time"))
        if current_latest is None or (
            isinstance(start_time, datetime)
            and isinstance(current_latest, datetime)
            and start_time > current_latest
        ):
            latest = start_time
        else:
            latest = current_latest
        candidates_by_prefix[parsed_ref.pod_prefix][normalized_job_url] = (
            current_count + 1,
            latest,
        )

    selected: dict[str, str] = {}
    ambiguous_prefixes: set[str] = set()
    for pod_prefix, variants in candidates_by_prefix.items():
        ranks = {url: (count, latest or datetime.min) for url, (count, latest) in variants.items()}
        best = max(ranks.values())
        winners = [url for url, rank in ranks.items() if rank == best]
        if len(winners) == 1:
            selected[pod_prefix] = winners[0]
        else:
            ambiguous_prefixes.add(pod_prefix)
            logger.info(
                "Pod prefix attribution unresolved",
                extra={"reason": "ambiguous_prefix", "cloud_phase": cloud_phase},
            )
    return _JenkinsPrefixMap(
        selected,
        cloud_phase=cloud_phase,
        reference_time=resolved_reference_time,
        ambiguous_prefixes=ambiguous_prefixes,
    )


def _resolve_jenkins_build_metadata(
    *,
    scheduled_at: datetime | None,
    candidate_urls: list[str],
    build_candidates_by_url: dict[str, list[dict[str, Any]]],
    retain_unmatched_url: bool = True,
    primary_url: str | None = None,
) -> dict[str, Any]:
    normalized_candidates = list(
        dict.fromkeys(candidate for candidate in candidate_urls if candidate)
    )
    matched = [
        {**build, "normalized_build_url": url}
        for url in normalized_candidates
        for build in build_candidates_by_url.get(url, [])
        if _within_build_time_guard(build, scheduled_at)
    ]
    resolved = _unique_build_metadata(matched, build_system="JENKINS")
    if resolved:
        if (
            resolved.get("_matched_build")
            and primary_url
            and resolved["normalized_build_url"] != primary_url
        ):
            return {"attribution_reason": "conflicting_candidate_urls"}
        return resolved
    retained = primary_url or (
        normalized_candidates[0] if len(normalized_candidates) == 1 else None
    )
    if retained and retain_unmatched_url:
        return {"build_system": "JENKINS", "normalized_build_url": retained}
    return {
        "attribution_reason": "missing_reference_time"
        if scheduled_at is None
        else "no_consistent_build"
    }


def _extract_normalized_build_url_from_pod_name(
    pod_name: str | None,
    pod_name_url_prefixes: dict[str, str],
) -> str | None:
    parsed_ref = _parse_jenkins_pod_name_build_ref(pod_name)
    if parsed_ref is None:
        return None
    normalized_job_url = pod_name_url_prefixes.get(parsed_ref.pod_prefix)
    if normalized_job_url is None:
        return None
    return f"{normalized_job_url}{parsed_ref.build_number}/"


def _parse_jenkins_pod_name_build_ref(pod_name: str | None) -> JenkinsPodNameBuildRef | None:
    normalized = _coerce_str(pod_name)
    if normalized is None:
        return None
    segments = [segment for segment in normalized.split("-") if segment]
    if len(segments) < 3:
        return None
    for index in range(len(segments) - 2, -1, -1):
        candidate = segments[index]
        if not NUMERIC_SEGMENT_RE.fullmatch(candidate):
            continue
        if len(candidate) < POD_NAME_BUILD_NUMBER_MIN_DIGITS:
            continue
        pod_prefix = "-".join(segments[:index]).strip("-")
        if pod_prefix == "":
            continue
        return JenkinsPodNameBuildRef(pod_prefix=pod_prefix, build_number=candidate)
    return None


def _jenkins_metadata_evidence(
    pod_metadata: PodMetadataSnapshot | None,
    *,
    cloud_phase: str,
) -> tuple[str | None, str | None]:
    if pod_metadata is None:
        return None, None
    urls = set()
    for name in ("buildUrl", "runUrl"):
        raw = _coerce_str(pod_metadata.annotations.get(name))
        if raw is None:
            continue
        normalized = _normalize_jenkins_runtime_url(raw, cloud_phase=cloud_phase)
        if normalized is None:
            return None, "invalid_or_cross_scope_metadata_url"
        urls.add(normalized)
    if len(urls) > 1:
        return None, "conflicting_metadata_urls"
    return next(iter(urls), None), None


def _extract_normalized_build_url_from_metadata(
    pod_metadata: PodMetadataSnapshot | None,
    *,
    cloud_phase: str = "GCP",
) -> str | None:
    url, reason = _jenkins_metadata_evidence(pod_metadata, cloud_phase=cloud_phase)
    if reason or pod_metadata is None:
        return None
    if url:
        return url
    return _build_jenkins_url_from_label_and_ci_job(
        org=_coerce_str(pod_metadata.labels.get("org")),
        repo=_coerce_str(pod_metadata.labels.get("repo")),
        ci_job=_coerce_str(pod_metadata.annotations.get("ci_job")),
        jenkins_label=_coerce_str(pod_metadata.labels.get("jenkins/label")),
        cloud_phase=cloud_phase,
    )


def _infer_pod_build_system(namespace_name: str | None) -> str:
    namespace = str(namespace_name or "").strip()
    if namespace.startswith("jenkins-"):
        return "JENKINS"
    if namespace == "prow-test-pods":
        return "PROW_NATIVE"
    return "UNKNOWN"


def _pod_identity_from_values(
    *,
    source_project: str,
    namespace_name: str | None,
    pod_uid: str | None,
    pod_name: str | None,
) -> tuple[str, str | None, str | None, str | None]:
    return (source_project, namespace_name, pod_uid, pod_name)


def _empty_pod_metadata_fields() -> dict[str, Any]:
    return {
        "pod_labels_json": None,
        "pod_annotations_json": None,
        "metadata_observed_at": None,
        "pod_created_at": None,
        "abnormal_reason": None,
        "abnormal_message": None,
        "pod_author": None,
        "pod_org": None,
        "pod_repo": None,
        "jenkins_label": None,
        "ci_job": None,
    }


def _load_pod_metadata_snapshots(
    pods: list[tuple[str, str | None, str | None, str | None]],
) -> dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot]:
    requested_by_namespace: dict[str, set[str]] = defaultdict(set)
    identities_by_namespace_name: dict[tuple[str, str], list[tuple[str, str | None, str | None, str | None]]] = (
        defaultdict(list)
    )
    for identity in pods:
        source_project, namespace_name, _, pod_name = identity
        if cloud_phase_for_source(source_project) != "GCP":
            continue
        if _infer_pod_build_system(namespace_name) not in {"JENKINS", "PROW_NATIVE"}:
            continue
        if namespace_name is None or pod_name is None:
            continue
        requested_by_namespace[namespace_name].add(pod_name)
        identities_by_namespace_name[(namespace_name, pod_name)].append(identity)

    if not requested_by_namespace:
        return {}

    base_url = _get_kubernetes_api_url()
    token = _get_kubernetes_api_token()
    ca_file = _get_kubernetes_api_ca_file()
    observed_at = datetime.now(UTC).replace(tzinfo=None)

    results: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot] = {}
    for namespace_name, pod_names in requested_by_namespace.items():
        live_snapshots = _list_namespace_pod_metadata(
            namespace_name=namespace_name,
            requested_pod_names=pod_names,
            base_url=base_url,
            token=token,
            ca_file=ca_file,
            observed_at=observed_at,
        )
        for pod_name, snapshot in live_snapshots.items():
            for identity in identities_by_namespace_name.get((namespace_name, pod_name), []):
                _, _, requested_pod_uid, _ = identity
                if requested_pod_uid is not None and snapshot.pod_uid is not None and requested_pod_uid != snapshot.pod_uid:
                    continue
                results[identity] = snapshot
    return results


def _get_kubernetes_api_url() -> str:
    from_env = _coerce_str(os.environ.get("CI_DASHBOARD_KUBERNETES_API_URL"))
    if from_env:
        return from_env.rstrip("/")

    host = _coerce_str(os.environ.get("KUBERNETES_SERVICE_HOST"))
    port = _coerce_str(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS")) or "443"
    if host is None:
        raise RuntimeError("Unable to resolve Kubernetes API host for sync-pods metadata fetch")
    return f"https://{host}:{port}"


def _get_kubernetes_api_token() -> str:
    from_env = _coerce_str(os.environ.get("CI_DASHBOARD_KUBERNETES_BEARER_TOKEN"))
    if from_env:
        return from_env

    try:
        return _read_text_file(KUBERNETES_SERVICE_ACCOUNT_TOKEN_PATH)
    except OSError as exc:
        raise RuntimeError("Unable to read Kubernetes service account token for sync-pods") from exc


def _get_kubernetes_api_ca_file() -> str | None:
    from_env = _coerce_str(os.environ.get("CI_DASHBOARD_KUBERNETES_CA_FILE"))
    if from_env:
        return from_env
    return KUBERNETES_SERVICE_ACCOUNT_CA_PATH if os.path.exists(KUBERNETES_SERVICE_ACCOUNT_CA_PATH) else None


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def _list_namespace_pod_metadata(
    *,
    namespace_name: str,
    requested_pod_names: set[str],
    base_url: str,
    token: str,
    ca_file: str | None,
    observed_at: datetime,
) -> dict[str, PodMetadataSnapshot]:
    if not requested_pod_names:
        return {}

    pod_items: dict[str, dict[str, Any]] = {}
    requested_claim_names: set[str] = set()
    continue_token: str | None = None
    while True:
        query = {
            "labelSelector": _pod_metadata_label_selector(namespace_name),
            "limit": "500",
        }
        if continue_token:
            query["continue"] = continue_token
        url = (
            f"{base_url}/api/v1/namespaces/{urllib_parse.quote(namespace_name, safe='')}/pods"
            f"?{urllib_parse.urlencode(query)}"
        )
        response = _get_json(url, headers={"Authorization": f"Bearer {token}"}, ca_file=ca_file)
        items = response.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                pod_name = _coerce_str(metadata.get("name"))
                if pod_name is None or pod_name not in requested_pod_names:
                    continue
                pod_items[pod_name] = item
                requested_claim_names.update(_persistent_volume_claim_names(item))
        metadata_obj = response.get("metadata")
        continue_token = None
        if isinstance(metadata_obj, dict):
            continue_token = _coerce_str(metadata_obj.get("continue"))
        if continue_token is None or requested_pod_names.issubset(set(pod_items)):
            break

    persistent_volume_names = _list_namespace_persistent_volume_names(
        namespace_name=namespace_name,
        requested_claim_names=requested_claim_names,
        base_url=base_url,
        token=token,
        ca_file=ca_file,
    )
    snapshots: dict[str, PodMetadataSnapshot] = {}
    for pod_name, item in pod_items.items():
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        volume_names = [
            persistent_volume_names[claim_name]
            for claim_name in _persistent_volume_claim_names(item)
            if persistent_volume_names.get(claim_name) is not None
        ]
        snapshots[pod_name] = PodMetadataSnapshot(
            pod_uid=_coerce_str(metadata.get("uid")),
            labels=_coerce_str_mapping(metadata.get("labels")),
            annotations=_coerce_str_mapping(metadata.get("annotations")),
            observed_at=observed_at,
            creation_timestamp=_parse_datetime(metadata.get("creationTimestamp")),
            persistent_volume_names=tuple(volume_names),
            **_extract_pod_abnormal_summary(item, observed_at=observed_at),
        )
    return snapshots


def _pod_metadata_label_selector(namespace_name: str) -> str:
    if _infer_pod_build_system(namespace_name) == "PROW_NATIVE":
        return "created-by-prow=true"
    return "jenkins/jenkins-jenkins-agent=true"


def _persistent_volume_claim_names(pod: dict[str, Any]) -> tuple[str, ...]:
    spec = pod.get("spec")
    if not isinstance(spec, dict):
        return ()
    volumes = spec.get("volumes")
    if not isinstance(volumes, list):
        return ()
    claim_names: list[str] = []
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        claim = volume.get("persistentVolumeClaim")
        if not isinstance(claim, dict):
            continue
        claim_name = _coerce_str(claim.get("claimName"))
        if claim_name is not None and claim_name not in claim_names:
            claim_names.append(claim_name)
    return tuple(claim_names)


def _list_namespace_persistent_volume_names(
    *,
    namespace_name: str,
    requested_claim_names: set[str],
    base_url: str,
    token: str,
    ca_file: str | None,
) -> dict[str, str | None]:
    if not requested_claim_names:
        return {}

    persistent_volume_names: dict[str, str | None] = {}
    remaining_claim_names = set(requested_claim_names)
    continue_token: str | None = None
    while True:
        query = {"limit": "500"}
        if continue_token:
            query["continue"] = continue_token
        url = (
            f"{base_url}/api/v1/namespaces/{urllib_parse.quote(namespace_name, safe='')}"
            f"/persistentvolumeclaims?{urllib_parse.urlencode(query)}"
        )
        response = _get_json(url, headers={"Authorization": f"Bearer {token}"}, ca_file=ca_file)
        items = response.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                claim_name = _coerce_str(metadata.get("name"))
                if claim_name is None or claim_name not in remaining_claim_names:
                    continue
                spec = item.get("spec")
                persistent_volume_names[claim_name] = (
                    _coerce_str(spec.get("volumeName")) if isinstance(spec, dict) else None
                )
                remaining_claim_names.remove(claim_name)
        metadata_obj = response.get("metadata")
        continue_token = None
        if isinstance(metadata_obj, dict):
            continue_token = _coerce_str(metadata_obj.get("continue"))
        if continue_token is None or not remaining_claim_names:
            return persistent_volume_names


def _get_json(url: str, *, headers: dict[str, str], ca_file: str | None) -> dict[str, Any]:
    request = urllib_request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "ci-dashboard-sync-pods",
            **headers,
        },
    )
    context = ssl.create_default_context(cafile=ca_file) if ca_file else None
    return _request_json(
        request,
        timeout=30,
        error_context="Kubernetes API",
        context=context,
    )


def _normalize_jenkins_runtime_url(value: str | None, *, cloud_phase: str = "GCP") -> str | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    host = jenkins_host_for_cloud(cloud_phase)
    try:
        parsed = urllib_parse.urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname
            not in {
                "prow.tidb.net",
                "do.pingcap.net",
                "jenkins.jenkins.svc.cluster.local",
            }
            or parsed.username
            or parsed.password
        ):
            return None
    normalized = normalize_build_url(raw, default_host=host)
    return normalized if normalized and _url_in_scope(normalized, cloud_phase, "JENKINS") else None


def _build_jenkins_url_from_label_and_ci_job(
    *,
    org: str | None,
    repo: str | None,
    ci_job: str | None,
    jenkins_label: str | None,
    cloud_phase: str = "GCP",
) -> str | None:
    if org is None or repo is None or ci_job is None:
        return None
    prefix = f"{org}/{repo}/"
    if not ci_job.startswith(prefix):
        return None
    build_number = _extract_build_number_from_jenkins_label(jenkins_label)
    if build_number is None:
        return None
    job_tail = ci_job[len(prefix) :].strip("/")
    if job_tail == "":
        return None
    job_segments = [segment for segment in job_tail.split("/") if segment]
    path_segments = ["jenkins", "job", org, "job", repo]
    for segment in job_segments:
        path_segments.extend(("job", segment))
    path_segments.append(build_number)
    return normalize_build_url(
        "/" + "/".join(path_segments), default_host=jenkins_host_for_cloud(cloud_phase)
    )


def _extract_build_number_from_jenkins_label(value: str | None) -> str | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    segments = [segment for segment in re.split(r"[_-]+", raw) if segment]
    if len(segments) < 2:
        return None
    candidate = segments[-2]
    suffix = segments[-1]
    if not NUMERIC_SEGMENT_RE.fullmatch(candidate):
        return None
    # Be conservative for fallback matching: a trailing numeric suffix is ambiguous.
    if NUMERIC_SEGMENT_RE.fullmatch(suffix):
        return None
    return candidate


def _upsert_pod_lifecycle(connection: Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    statement = _build_pod_lifecycle_upsert_statement(connection)
    connection.execute(statement, rows)


def _build_pvc_pod_mapping_rows(
    lifecycle_rows: list[dict[str, Any]],
    *,
    pod_metadata_by_identity: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lifecycle in lifecycle_rows:
        source_project = _coerce_str(lifecycle.get("source_project"))
        namespace_name = _coerce_str(lifecycle.get("namespace_name"))
        pod_uid = _coerce_str(lifecycle.get("pod_uid"))
        pod_name = _coerce_str(lifecycle.get("pod_name"))
        if source_project is None or namespace_name is None or pod_uid is None or pod_name is None:
            continue
        identity = _pod_identity_from_values(
            source_project=source_project,
            namespace_name=namespace_name,
            pod_uid=pod_uid,
            pod_name=pod_name,
        )
        metadata = pod_metadata_by_identity.get(identity)
        if metadata is None:
            continue
        for volume_name in metadata.persistent_volume_names:
            rows.append(
                {
                    "vendor": "gcp",
                    "account_id": source_project,
                    "persistent_volume_name": volume_name,
                    "pod_uid": pod_uid,
                    "author": _coerce_str(lifecycle.get("pod_author")),
                    "org": _coerce_str(lifecycle.get("pod_org")),
                    "repo": _coerce_str(lifecycle.get("pod_repo")),
                }
            )
    return rows


def _upsert_pvc_pod_mappings(connection: Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    if connection.dialect.name == "sqlite":
        statement = text(
            """
            INSERT INTO cost_kubernetes_pvc_pod_mapping (
              vendor, account_id, persistent_volume_name, pod_uid, author, org, repo
            ) VALUES (
              :vendor, :account_id, :persistent_volume_name, :pod_uid, :author, :org, :repo
            )
            ON CONFLICT(
              vendor, account_id, persistent_volume_name, pod_uid
            ) DO UPDATE SET
              author = excluded.author,
              org = excluded.org,
              repo = excluded.repo,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    else:
        statement = text(
            """
            INSERT INTO cost_kubernetes_pvc_pod_mapping (
              vendor, account_id, persistent_volume_name, pod_uid, author, org, repo
            ) VALUES (
              :vendor, :account_id, :persistent_volume_name, :pod_uid, :author, :org, :repo
            )
            ON DUPLICATE KEY UPDATE
              author = VALUES(author),
              org = VALUES(org),
              repo = VALUES(repo),
              updated_at = CURRENT_TIMESTAMP
            """
        )
    connection.execute(statement, rows)


def _reconcile_recent_lifecycle_rows(
    connection: Connection,
    *,
    batch_size: int | None = None,
    jenkins_pod_name_url_prefixes: dict[str, str] | None = None,
    reference_time: datetime | None = None,
) -> int:
    window_hours = _read_int_env(
        "CI_DASHBOARD_POD_LINK_RECONCILE_WINDOW_HOURS", POD_LINK_RECONCILE_WINDOW_HOURS
    )
    end = _parse_datetime(reference_time) or datetime.now(UTC).replace(tzinfo=None)
    cutoff = end - timedelta(hours=window_hours)
    return _reconcile_lifecycle_rows_in_time_window(
        connection,
        start_time_from=cutoff,
        start_time_to=end,
        gcp_only=True,
        batch_size=batch_size,
        jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
    )


def _reconcile_lifecycle_rows_in_time_window(
    connection: Connection,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None,
    batch_size: int | None = None,
    jenkins_pod_name_url_prefixes: dict[str, str] | None = None,
    source_project: str | None = None,
    gcp_only: bool = False,
) -> int:
    """Connection-only sweep: transaction ownership always stays with the caller."""
    size = (
        batch_size
        if batch_size is not None
        else _read_int_env(
            "CI_DASHBOARD_POD_LINK_RECONCILE_BATCH_SIZE", POD_LINK_RECONCILE_BATCH_SIZE
        )
    )
    if size <= 0:
        raise ValueError("Pod reconciliation page size must be positive")
    end = _parse_datetime(start_time_to) or datetime.now(UTC).replace(tzinfo=None)
    start = _parse_datetime(start_time_from)
    if start is None or start >= end:
        raise ValueError("Pod reconciliation requires a nonempty [from, to) UTC window")
    through_id = _freeze_reconcile_ceiling(
        connection,
        start_time_from=start,
        start_time_to=end,
        source_project=source_project,
        gcp_only=gcp_only,
    )
    after_id = total_updated = 0
    while True:
        rows = _load_lifecycle_rows_for_reconcile(
            connection,
            start_time_from=start,
            start_time_to=end,
            after_lifecycle_id=after_id,
            through_id=through_id,
            limit=size,
            source_project=source_project,
            gcp_only=gcp_only,
        )
        if not rows:
            return total_updated
        updated, _, _ = _reconcile_lifecycle_batch(
            connection,
            rows,
            jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
        )
        total_updated += updated
        after_id = int(rows[-1]["id"])


def _reconcile_lifecycle_batch(
    connection: Connection,
    lifecycle_rows: list[dict[str, Any]],
    *,
    jenkins_pod_name_url_prefixes: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    metadata = _load_build_metadata_map(
        connection,
        lifecycle_rows,
        pod_metadata_by_identity=_deserialize_pod_metadata_snapshots_from_lifecycle_rows(
            lifecycle_rows
        ),
        jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
    )
    guarded_fields = _LINKAGE_FIELDS + ("metadata_observed_at",)
    guards = " AND ".join(
        _null_safe_equals_sql(field, f":old_{field}", connection.dialect.name)
        for field in guarded_fields
    )
    statement = text(
        "UPDATE ci_l1_pod_lifecycle SET "
        + ", ".join(f"{field} = :{field}" for field in _LINKAGE_FIELDS)
        + f", updated_at = CURRENT_TIMESTAMP WHERE id = :id AND source_project = :source_project AND {guards}"
    )
    updated = ambiguous = skipped = 0
    for row in lifecycle_rows:
        identity = (row["source_project"], row["namespace_name"], row["pod_uid"], row["pod_name"])
        build = metadata.get(identity, {})
        reason = build.get("attribution_reason")
        if reason:
            if any(
                category in reason
                for category in ("ambiguous", "conflict", "contradict", "collision")
            ):
                ambiguous += 1
            else:
                skipped += 1
            continue
        if not build.get("_matched_build"):
            skipped += 1
            continue
        resolved = {
            **{field: _coerce_str(build.get(field)) for field in _LINKAGE_FIELDS},
            "job_name": canonicalize_job_name(
                _coerce_str(build.get("job_name")),
                repo_full_name=_coerce_str(build.get("repo_full_name")),
            ),
            "pod_author": _coerce_str(build.get("author")),
            "pod_org": _coerce_str(build.get("org")),
            "pod_repo": _coerce_str(build.get("repo")),
            "ci_job": _build_ci_job_from_build_metadata(build)
            if build.get("build_system") == "JENKINS"
            else None,
        }
        payload = {
            field: row.get(field)
            if _coerce_str(row.get(field)) is not None
            and not (field == "build_system" and row.get(field) == "UNKNOWN")
            else resolved.get(field)
            for field in _LINKAGE_FIELDS
        }
        if all(payload[field] == row.get(field) for field in _LINKAGE_FIELDS):
            skipped += 1
            continue
        payload.update(
            {
                "id": row["id"],
                "source_project": row["source_project"],
                **{f"old_{field}": row.get(field) for field in guarded_fields},
            }
        )
        result = connection.execute(statement, payload)
        if result.rowcount == 1:
            updated += 1
        else:
            skipped += 1
    return updated, ambiguous, skipped


def _reconcile_scope_sql(source_project: str | None, gcp_only: bool = False) -> str:
    clauses = []
    if source_project is not None:
        clauses.append("source_project = :source_project")
    if gcp_only:
        clauses.append("LOWER(source_project) NOT LIKE 'tencent:%'")
    return " AND ".join(clauses) or "1 = 1"


def _freeze_reconcile_ceiling(
    connection: Connection,
    *,
    start_time_from: datetime,
    start_time_to: datetime,
    source_project: str | None = None,
    gcp_only: bool = False,
) -> int:
    reference = "COALESCE(scheduled_at, pod_created_at)"
    lower = _datetime_lower_bound_sql(reference, ":start_time_from", connection.dialect.name)
    upper = _datetime_upper_bound_sql(reference, ":start_time_to", connection.dialect.name)
    return int(
        connection.execute(
            text(
                f"SELECT COALESCE(MAX(id), 0) FROM ci_l1_pod_lifecycle WHERE {lower} AND {upper} AND {_reconcile_scope_sql(source_project, gcp_only)}"
            ),
            {
                "start_time_from": start_time_from,
                "start_time_to": start_time_to,
                "source_project": source_project,
            },
        ).scalar_one()
    )


def _count_reconcile_missing_time_rows(
    connection: Connection,
    *,
    source_project: str | None,
) -> int:
    # Undatable rows cannot belong to a date window; report a source-wide
    # diagnostic separately from the selected/scanned row counts and cursor.
    return int(
        connection.execute(
            text(
                "SELECT COUNT(*) FROM ci_l1_pod_lifecycle WHERE scheduled_at IS NULL AND pod_created_at IS NULL "
                f"AND {_reconcile_scope_sql(source_project)} AND {_INCOMPLETE_LINKAGE_SQL}"
            ),
            {"source_project": source_project},
        ).scalar_one()
    )


_INCOMPLETE_LINKAGE_SQL = """(
    NULLIF(source_prow_job_id, '') IS NULL OR NULLIF(normalized_build_url, '') IS NULL
    OR NULLIF(repo_full_name, '') IS NULL OR NULLIF(job_name, '') IS NULL
    OR ((build_system = 'JENKINS' OR namespace_name LIKE 'jenkins-%') AND (
        NULLIF(pod_author, '') IS NULL OR NULLIF(pod_org, '') IS NULL
        OR NULLIF(pod_repo, '') IS NULL OR NULLIF(ci_job, '') IS NULL
    ))
)"""


def _load_lifecycle_rows_for_reconcile(
    connection: Connection,
    *,
    start_time_from: datetime,
    start_time_to: datetime | None,
    after_lifecycle_id: int,
    limit: int,
    through_id: int | None = None,
    source_project: str | None = None,
    gcp_only: bool = False,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "start_time_from": start_time_from,
        "after_lifecycle_id": after_lifecycle_id,
        "through_id": through_id,
        "source_project": source_project,
        "limit": limit,
    }
    reference = "COALESCE(scheduled_at, pod_created_at)"
    end_clause = ""
    if start_time_to is not None:
        end_clause = f" AND {_datetime_upper_bound_sql(reference, ':start_time_to', connection.dialect.name)}"
        params["start_time_to"] = start_time_to
    start_time_from_clause = _datetime_lower_bound_sql(
        reference, ":start_time_from", connection.dialect.name
    )

    rows = connection.execute(
        text(
            f"""
            SELECT
              id,
              source_project,
              namespace_name,
              pod_uid,
              pod_name,
              build_system,
              pod_labels_json,
              pod_annotations_json,
              metadata_observed_at,
              pod_created_at,
              scheduled_at,
              pod_author,
              pod_org,
              pod_repo,
              ci_job,
              source_prow_job_id,
              normalized_build_url,
              repo_full_name,
              job_name
            FROM ci_l1_pod_lifecycle
            WHERE {start_time_from_clause}
{end_clause} AND id > :after_lifecycle_id
              AND (:through_id IS NULL OR id <= :through_id)
              AND {_reconcile_scope_sql(source_project, gcp_only)}
              AND {_INCOMPLETE_LINKAGE_SQL}
            ORDER BY id ASC
            LIMIT :limit
            """
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def _deserialize_pod_metadata_snapshots_from_lifecycle_rows(
    lifecycle_rows: list[dict[str, Any]],
) -> dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot]:
    snapshots: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot] = {}
    for lifecycle in lifecycle_rows:
        labels = _json_loads_str_mapping(lifecycle.get("pod_labels_json"))
        annotations = _json_loads_str_mapping(lifecycle.get("pod_annotations_json"))
        creation_timestamp = _parse_datetime(lifecycle.get("pod_created_at"))
        abnormal_reason = _coerce_str(lifecycle.get("abnormal_reason"))
        abnormal_message = _coerce_str(lifecycle.get("abnormal_message"))
        if (
            not labels
            and not annotations
            and creation_timestamp is None
            and abnormal_reason is None
            and abnormal_message is None
            and _parse_datetime(lifecycle.get("metadata_observed_at")) is None
        ):
            continue
        identity = _pod_identity_from_values(
            source_project=_coerce_str(lifecycle.get("source_project")) or "",
            namespace_name=_coerce_str(lifecycle.get("namespace_name")),
            pod_uid=_coerce_str(lifecycle.get("pod_uid")),
            pod_name=_coerce_str(lifecycle.get("pod_name")),
        )
        observed_at = _parse_datetime(lifecycle.get("metadata_observed_at"))
        snapshots[identity] = PodMetadataSnapshot(
            pod_uid=_coerce_str(lifecycle.get("pod_uid")),
            labels=labels,
            annotations=annotations,
            observed_at=observed_at,
            creation_timestamp=creation_timestamp,
            abnormal_reason=abnormal_reason,
            abnormal_message=abnormal_message,
        )
    return snapshots


def _json_loads_str_mapping(value: Any) -> dict[str, str]:
    raw = _coerce_str(value)
    if raw is None:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return _coerce_str_mapping(loaded)


def _build_pod_lifecycle_upsert_statement(connection: Connection):
    sqlite = connection.dialect.name == "sqlite"
    columns = (
        "source_project",
        "cluster_name",
        "location",
        "namespace_name",
        "pod_name",
        "pod_uid",
        "build_system",
        "pod_labels_json",
        "pod_annotations_json",
        "metadata_observed_at",
        "pod_created_at",
        "abnormal_reason",
        "abnormal_message",
        "pod_author",
        "pod_org",
        "pod_repo",
        "jenkins_label",
        "ci_job",
        "source_prow_job_id",
        "normalized_build_url",
        "repo_full_name",
        "job_name",
        *_FIRST_MILESTONE_FIELDS,
        "last_failed_scheduling_at",
        "failed_scheduling_count",
        "last_event_at",
        "schedule_to_started_seconds",
    )

    def old(column: str) -> str:
        return f"ci_l1_pod_lifecycle.{column}"

    def new(column: str) -> str:
        return f"excluded.{column}" if sqlite else f"VALUES({column})"

    def extrema(column: str, *, earliest: bool) -> str:
        left, right = old(column), new(column)
        operator = "<" if earliest else ">"
        comparison = f"{right} {operator} {left}"
        if sqlite and column != "failed_scheduling_count":
            comparison = f"julianday({right}) {operator} julianday({left})"
        return f"CASE WHEN {left} IS NULL THEN {right} WHEN {right} IS NULL THEN {left} WHEN {comparison} THEN {right} ELSE {left} END"

    observed, incoming = old("metadata_observed_at"), new("metadata_observed_at")
    observed_time = f"julianday({observed})" if sqlite else observed
    incoming_time = f"julianday({incoming})" if sqlite else incoming
    newer_snapshot = (
        f"({incoming} IS NOT NULL AND ({observed} IS NULL OR {incoming_time} > {observed_time}))"
    )
    stale_snapshot = (
        f"({observed} IS NOT NULL AND ({incoming} IS NULL OR {incoming_time} < {observed_time}))"
    )
    consistent_link = " AND ".join(
        f"(NULLIF({old(field)}, '') IS NULL OR NULLIF({new(field)}, '') IS NULL OR {old(field)} = {new(field)})"
        for field in ("normalized_build_url", "source_prow_job_id")
    )
    updates = {}
    for field in ("cluster_name", "location"):
        updates[field] = f"COALESCE({new(field)}, {old(field)})"
    for field in ("pod_labels_json", "pod_annotations_json", "jenkins_label"):
        updates[field] = f"CASE WHEN {newer_snapshot} THEN {new(field)} ELSE {old(field)} END"
    updates["pod_created_at"] = f"COALESCE({old('pod_created_at')}, {new('pod_created_at')})"
    for field in ("pod_author", "pod_org", "pod_repo", "ci_job"):
        updates[field] = (
            f"CASE WHEN NOT ({consistent_link}) THEN {old(field)} "
            f"WHEN {newer_snapshot} THEN {new(field)} "
            f"WHEN {stale_snapshot} THEN {old(field)} "
            f"ELSE COALESCE(NULLIF({old(field)}, ''), {new(field)}) END"
        )
    # Link guards must see the old URL AND ID on MySQL, whose assignments run left to right.
    for field in ("repo_full_name", "job_name", "build_system"):
        missing = f"NULLIF({old(field)}, '') IS NULL"
        if field == "build_system":
            missing += f" OR {old(field)} = 'UNKNOWN'"
        updates[field] = (
            f"CASE WHEN ({missing}) AND ({consistent_link}) THEN {new(field)} ELSE {old(field)} END"
        )
    updates["source_prow_job_id"] = (
        f"CASE WHEN {consistent_link} THEN COALESCE(NULLIF({old('source_prow_job_id')}, ''), {new('source_prow_job_id')}) ELSE {old('source_prow_job_id')} END"
    )
    updates["normalized_build_url"] = (
        f"CASE WHEN {consistent_link} THEN COALESCE(NULLIF({old('normalized_build_url')}, ''), {new('normalized_build_url')}) ELSE {old('normalized_build_url')} END"
    )
    for field in _FIRST_MILESTONE_FIELDS + _LATEST_MILESTONE_FIELDS:
        updates[field] = extrema(field, earliest=field in _FIRST_MILESTONE_FIELDS)
    updates["failed_scheduling_count"] = extrema("failed_scheduling_count", earliest=False)
    scheduled = extrema("scheduled_at", earliest=True)
    started = extrema("first_started_at", earliest=True)
    duration = (
        f"CAST(strftime('%s', ({started})) AS INTEGER) - CAST(strftime('%s', ({scheduled})) AS INTEGER)"
        if sqlite
        else f"TIMESTAMPDIFF(SECOND, ({scheduled}), ({started}))"
    )
    nonnegative = (
        f"julianday({started}) >= julianday({scheduled})"
        if sqlite
        else f"({started}) >= ({scheduled})"
    )
    updates["schedule_to_started_seconds"] = f"CASE WHEN {nonnegative} THEN {duration} END"
    old_reason, new_reason = old("abnormal_reason"), new("abnormal_reason")

    def reason_rank(expression: str) -> str:
        return (
            "CASE "
            + expression
            + " "
            + " ".join(
                f"WHEN '{reason}' THEN {rank}" for reason, rank in ABNORMAL_REASON_RANK.items()
            )
            + " ELSE 999 END"
        )

    prefer_abnormal = (
        f"{new_reason} IS NOT NULL AND ({old_reason} IS NULL OR "
        f"{reason_rank(new_reason)} < {reason_rank(old_reason)} OR "
        f"({new_reason} = {old_reason} AND {new('abnormal_message')} IS NOT NULL))"
    )
    updates["abnormal_message"] = (
        f"CASE WHEN {prefer_abnormal} THEN {new('abnormal_message')} ELSE {old('abnormal_message')} END"
    )
    updates["abnormal_reason"] = (
        f"CASE WHEN {prefer_abnormal} THEN {new_reason} ELSE {old_reason} END"
    )
    # Keep observation ordering predicates on the original timestamp in MySQL/TiDB.
    updates["metadata_observed_at"] = (
        f"CASE WHEN {newer_snapshot} THEN {incoming} ELSE {observed} END"
    )
    updates["updated_at"] = "CURRENT_TIMESTAMP"
    conflict = (
        "ON CONFLICT(source_project, namespace_name, pod_uid, pod_name) DO UPDATE SET"
        if sqlite
        else "ON DUPLICATE KEY UPDATE"
    )
    return text(
        f"INSERT INTO ci_l1_pod_lifecycle ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + field for field in columns)}) {conflict} "
        + ",\n".join(f"{field} = {expression}" for field, expression in updates.items())
    )
