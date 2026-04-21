from __future__ import annotations

import json
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
from ci_dashboard.jobs.build_url_matcher import classify_build_system, normalize_build_url
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
    "FailedScheduling",
)

DEFAULT_POD_EVENT_NAMESPACES = (
    "prow-test-pods",
    "jenkins-tidb",
    "jenkins-tiflow",
)

NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
API_RETRY_ATTEMPTS = 3
API_RETRY_BASE_DELAY_SECONDS = 1.0


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
    observed_at: datetime

    def as_lifecycle_fields(self) -> dict[str, Any]:
        return {
            "pod_labels_json": _json_dumps_or_none(self.labels),
            "pod_annotations_json": _json_dumps_or_none(self.annotations),
            "metadata_observed_at": self.observed_at,
            "pod_author": _coerce_str(self.labels.get("author")),
            "pod_org": _coerce_str(self.labels.get("org")),
            "pod_repo": _coerce_str(self.labels.get("repo")),
            "jenkins_label": _coerce_str(self.labels.get("jenkins/label")),
            "jenkins_label_digest": _coerce_str(self.labels.get("jenkins/label-digest")),
            "jenkins_controller": _coerce_str(self.labels.get("kubernetes.jenkins.io/controller")),
            "ci_job": _coerce_str(self.annotations.get("ci_job")),
        }


def run_sync_pods(engine: Engine, settings: Settings) -> SyncPodsSummary:
    summary = SyncPodsSummary()
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
            for group in chunked(sorted(affected_pods), settings.jobs.batch_size):
                with engine.begin() as connection:
                    lifecycle_rows = _build_lifecycle_rows(
                        connection,
                        group,
                        pod_metadata_by_identity=pod_metadata_by_identity,
                    )
                    if lifecycle_rows:
                        _upsert_pod_lifecycle(connection, lifecycle_rows)
                        summary.lifecycle_rows_upserted += len(lifecycle_rows)
            summary.pods_touched = len(affected_pods)

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
        first_timestamp=_parse_datetime(payload.get("firstTimestamp")),
        last_timestamp=_parse_datetime(payload.get("lastTimestamp")),
        reporting_component=_coerce_str(payload.get("reportingComponent")),
        reporting_instance=_coerce_str(payload.get("reportingInstance")),
        source_insert_id=source_insert_id,
    )


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
              event_timestamp = excluded.event_timestamp,
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
          event_timestamp = VALUES(event_timestamp),
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
    pod_metadata_by_identity: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot],
) -> list[dict[str, Any]]:
    lifecycle_rows = _load_lifecycle_aggregates(connection, pods)
    if not lifecycle_rows:
        return []

    build_metadata_by_identity = _load_build_metadata_map(
        connection,
        lifecycle_rows,
        pod_metadata_by_identity=pod_metadata_by_identity,
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
        pod_metadata = pod_metadata_by_identity.get(identity)
        build_meta = build_metadata_by_identity.get(identity, {})
        merged = {
            **lifecycle,
            **(pod_metadata.as_lifecycle_fields() if pod_metadata else _empty_pod_metadata_fields()),
            "build_system": build_meta.get("build_system") or _infer_pod_build_system(namespace_name),
            "jenkins_build_url_key": build_meta.get("jenkins_build_url_key"),
            "source_prow_job_id": build_meta.get("source_prow_job_id"),
            "normalized_build_key": build_meta.get("normalized_build_key"),
            "repo_full_name": build_meta.get("repo_full_name"),
            "job_name": build_meta.get("job_name"),
        }
        schedule_at = _parse_datetime(merged.get("scheduled_at"))
        started_at = _parse_datetime(merged.get("first_started_at"))
        merged["scheduled_at"] = schedule_at
        merged["first_started_at"] = started_at
        if isinstance(schedule_at, datetime) and isinstance(started_at, datetime):
            merged["schedule_to_started_seconds"] = int((started_at - schedule_at).total_seconds())
        else:
            merged["schedule_to_started_seconds"] = None
        rows.append(merged)
    return rows


def _load_lifecycle_aggregates(
    connection: Connection,
    pods: list[tuple[str, str | None, str | None, str | None]],
) -> list[dict[str, Any]]:
    if not pods:
        return []

    requested_pods_sql, params = _build_requested_pods_relation(pods)
    statement = text(
        f"""
        WITH requested_pods AS (
          {requested_pods_sql}
        )
        SELECT
          events.source_project,
          events.cluster_name,
          events.location,
          events.namespace_name,
          events.pod_name,
          events.pod_uid,
          MIN(CASE WHEN events.event_reason = 'Scheduled' THEN events.event_timestamp END) AS scheduled_at,
          MIN(CASE WHEN events.event_reason = 'Pulling' THEN events.event_timestamp END) AS first_pulling_at,
          MIN(CASE WHEN events.event_reason = 'Pulled' THEN events.event_timestamp END) AS first_pulled_at,
          MIN(CASE WHEN events.event_reason = 'Created' THEN events.event_timestamp END) AS first_created_at,
          MIN(CASE WHEN events.event_reason = 'Started' THEN events.event_timestamp END) AS first_started_at,
          MAX(CASE WHEN events.event_reason = 'FailedScheduling' THEN events.event_timestamp END) AS last_failed_scheduling_at,
          SUM(CASE WHEN events.event_reason = 'FailedScheduling' THEN 1 ELSE 0 END) AS failed_scheduling_count,
          MAX(events.event_timestamp) AS last_event_at
        FROM ci_l1_pod_events AS events
        JOIN requested_pods AS requested
          ON events.source_project = requested.source_project
         AND {_null_safe_equals_sql('events.namespace_name', 'requested.namespace_name', connection.dialect.name)}
         AND {_null_safe_equals_sql('events.pod_uid', 'requested.pod_uid', connection.dialect.name)}
         AND {_null_safe_equals_sql('events.pod_name', 'requested.pod_name', connection.dialect.name)}
        GROUP BY
          events.source_project,
          events.cluster_name,
          events.location,
          events.namespace_name,
          events.pod_name,
          events.pod_uid
        """
    )
    rows = connection.execute(statement, params).mappings()
    return [dict(row) for row in rows]


def _build_requested_pods_relation(
    pods: list[tuple[str, str | None, str | None, str | None]],
) -> tuple[str, dict[str, Any]]:
    selects: list[str] = []
    params: dict[str, Any] = {}
    for index, (source_project, namespace_name, pod_uid, pod_name) in enumerate(pods):
        selects.append(
            "SELECT "
            f":source_project_{index} AS source_project, "
            f":namespace_name_{index} AS namespace_name, "
            f":pod_uid_{index} AS pod_uid, "
            f":pod_name_{index} AS pod_name"
        )
        params[f"source_project_{index}"] = source_project
        params[f"namespace_name_{index}"] = namespace_name
        params[f"pod_uid_{index}"] = pod_uid
        params[f"pod_name_{index}"] = pod_name
    return "\nUNION ALL\n".join(selects), params


def _null_safe_equals_sql(left: str, right: str, dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return f"{left} IS {right}"
    return f"{left} <=> {right}"


def _load_build_metadata_map(
    connection: Connection,
    lifecycle_rows: list[dict[str, Any]],
    *,
    pod_metadata_by_identity: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot],
) -> dict[tuple[str, str | None, str | None, str | None], dict[str, Any]]:
    prow_identities: dict[tuple[str, str | None, str | None, str | None], str] = {}
    jenkins_pod_metadata: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot] = {}

    for row in lifecycle_rows:
        identity = _pod_identity_from_values(
            source_project=_coerce_str(row.get("source_project")) or "",
            namespace_name=_coerce_str(row.get("namespace_name")),
            pod_uid=_coerce_str(row.get("pod_uid")),
            pod_name=_coerce_str(row.get("pod_name")),
        )
        namespace_name = _coerce_str(row.get("namespace_name"))
        pod_name = _coerce_str(row.get("pod_name"))
        build_system = _infer_pod_build_system(namespace_name)
        if build_system == "PROW_NATIVE" and pod_name is not None:
            prow_identities[identity] = pod_name
        if build_system == "JENKINS" and identity in pod_metadata_by_identity:
            jenkins_pod_metadata[identity] = pod_metadata_by_identity[identity]

    results: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = {}

    if prow_identities:
        direct_by_pod_name = _load_direct_build_metadata_map(
            connection,
            pod_names=sorted(set(prow_identities.values())),
        )
        for identity, pod_name in prow_identities.items():
            if pod_name in direct_by_pod_name:
                results[identity] = direct_by_pod_name[pod_name]

    if jenkins_pod_metadata:
        results.update(
            _load_jenkins_build_metadata_map(
                connection,
                pod_metadata_by_identity=jenkins_pod_metadata,
            )
        )

    return results


def _load_direct_build_metadata_map(
    connection: Connection,
    *,
    pod_names: list[str],
) -> dict[str, dict[str, Any]]:
    if not pod_names:
        return {}

    placeholders: list[str] = []
    params: dict[str, Any] = {}
    for index, pod_name in enumerate(pod_names):
        param_name = f"pod_name_{index}"
        placeholders.append(f":{param_name}")
        params[param_name] = pod_name

    rows = connection.execute(
        text(
            f"""
            SELECT
              pod_name,
              source_prow_job_id,
              normalized_build_key,
              repo_full_name,
              job_name,
              url,
              start_time
            FROM ci_l1_builds
            WHERE pod_name IN ({", ".join(placeholders)})
            ORDER BY pod_name ASC, start_time DESC
            """
        ),
        params,
    ).mappings()

    metadata_by_pod_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        pod_name = _coerce_str(row.get("pod_name"))
        if pod_name is None or pod_name in metadata_by_pod_name:
            continue
        payload = dict(row)
        build_system = classify_build_system(payload.get("url"))
        metadata_by_pod_name[pod_name] = {
            "build_system": build_system,
            "jenkins_build_url_key": payload.get("normalized_build_key") if build_system == "JENKINS" else None,
            "source_prow_job_id": payload.get("source_prow_job_id"),
            "normalized_build_key": payload.get("normalized_build_key"),
            "repo_full_name": payload.get("repo_full_name"),
            "job_name": payload.get("job_name"),
        }
    return metadata_by_pod_name


def _load_jenkins_build_metadata_map(
    connection: Connection,
    *,
    pod_metadata_by_identity: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot],
) -> dict[tuple[str, str | None, str | None, str | None], dict[str, Any]]:
    build_keys_by_identity = {
        identity: build_key
        for identity, pod_metadata in pod_metadata_by_identity.items()
        for build_key in [_extract_jenkins_build_url_key_from_metadata(pod_metadata)]
        if build_key is not None
    }
    if not build_keys_by_identity:
        return {}

    build_keys = sorted(set(build_keys_by_identity.values()))
    params: dict[str, Any] = {}
    placeholders: list[str] = []
    for index, build_key in enumerate(build_keys):
        param_name = f"build_key_{index}"
        placeholders.append(f":{param_name}")
        params[param_name] = build_key

    candidates = list(
        connection.execute(
            text(
                f"""
                SELECT
                  source_prow_job_id,
                  normalized_build_key,
                  repo_full_name,
                  job_name,
                  start_time
                FROM ci_l1_builds
                WHERE normalized_build_key IS NOT NULL
                  AND normalized_build_key IN ({", ".join(placeholders)})
                ORDER BY start_time DESC
                """
            ),
            params,
        ).mappings()
    )
    candidates_by_build_key: dict[str, dict[str, Any]] = {}
    for candidate_row in candidates:
        candidate = dict(candidate_row)
        build_key = _coerce_str(candidate.get("normalized_build_key"))
        if build_key is None or build_key in candidates_by_build_key:
            continue
        candidates_by_build_key[build_key] = candidate

    results: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = {}
    for identity, build_key in build_keys_by_identity.items():
        payload: dict[str, Any] = {
            "build_system": "JENKINS",
            "jenkins_build_url_key": build_key,
        }
        matched_build = candidates_by_build_key.get(build_key)
        if matched_build is not None:
            payload.update(
                {
                    "source_prow_job_id": matched_build.get("source_prow_job_id"),
                    "normalized_build_key": matched_build.get("normalized_build_key"),
                    "repo_full_name": matched_build.get("repo_full_name"),
                    "job_name": matched_build.get("job_name"),
                }
            )
        results[identity] = payload
    return results


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
        "pod_author": None,
        "pod_org": None,
        "pod_repo": None,
        "jenkins_label": None,
        "jenkins_label_digest": None,
        "jenkins_controller": None,
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
        if _infer_pod_build_system(namespace_name) != "JENKINS":
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

    snapshots: dict[str, PodMetadataSnapshot] = {}
    continue_token: str | None = None
    while True:
        query = {
            "labelSelector": "jenkins/jenkins-jenkins-agent=true",
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
                snapshots[pod_name] = PodMetadataSnapshot(
                    pod_uid=_coerce_str(metadata.get("uid")),
                    labels=_coerce_str_mapping(metadata.get("labels")),
                    annotations=_coerce_str_mapping(metadata.get("annotations")),
                    observed_at=observed_at,
                )
        metadata_obj = response.get("metadata")
        continue_token = None
        if isinstance(metadata_obj, dict):
            continue_token = _coerce_str(metadata_obj.get("continue"))
        if continue_token is None or requested_pod_names.issubset(set(snapshots)):
            break
    return snapshots


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


def _extract_jenkins_build_url_key_from_metadata(pod_metadata: PodMetadataSnapshot) -> str | None:
    annotations = pod_metadata.annotations
    labels = pod_metadata.labels
    for raw_url in (
        annotations.get("buildUrl"),
        annotations.get("runUrl"),
    ):
        build_key = _normalize_jenkins_runtime_url(raw_url)
        if build_key is not None:
            return build_key
    return _build_jenkins_key_from_label_and_ci_job(
        org=_coerce_str(labels.get("org")),
        repo=_coerce_str(labels.get("repo")),
        ci_job=_coerce_str(annotations.get("ci_job")),
        jenkins_label=_coerce_str(labels.get("jenkins/label")),
    )


def _normalize_jenkins_runtime_url(value: str | None) -> str | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    parsed = urllib_parse.urlparse(raw)
    normalized = parsed.path or raw
    if not normalized.startswith("/"):
        normalized = f"/{normalized.lstrip('/')}"
    if normalized.startswith("/job/"):
        normalized = f"/jenkins{normalized}"
    normalized_key = normalize_build_url(normalized)
    if normalized_key is None:
        return None
    return normalized_key if normalized_key.startswith("/jenkins/job/") else None


def _build_jenkins_key_from_label_and_ci_job(
    *,
    org: str | None,
    repo: str | None,
    ci_job: str | None,
    jenkins_label: str | None,
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
    return "/" + "/".join(path_segments)


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


def _build_pod_lifecycle_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            """
            INSERT INTO ci_l1_pod_lifecycle (
              source_project, cluster_name, location, namespace_name, pod_name, pod_uid,
              build_system, pod_labels_json, pod_annotations_json, metadata_observed_at,
              pod_author, pod_org, pod_repo, jenkins_label, jenkins_label_digest, jenkins_controller, ci_job,
              jenkins_build_url_key, source_prow_job_id, normalized_build_key, repo_full_name, job_name,
              scheduled_at, first_pulling_at, first_pulled_at, first_created_at, first_started_at,
              last_failed_scheduling_at, failed_scheduling_count, last_event_at, schedule_to_started_seconds
            ) VALUES (
              :source_project, :cluster_name, :location, :namespace_name, :pod_name, :pod_uid,
              :build_system, :pod_labels_json, :pod_annotations_json, :metadata_observed_at,
              :pod_author, :pod_org, :pod_repo, :jenkins_label, :jenkins_label_digest, :jenkins_controller, :ci_job,
              :jenkins_build_url_key, :source_prow_job_id, :normalized_build_key, :repo_full_name, :job_name,
              :scheduled_at, :first_pulling_at, :first_pulled_at, :first_created_at, :first_started_at,
              :last_failed_scheduling_at, :failed_scheduling_count, :last_event_at, :schedule_to_started_seconds
            )
            ON CONFLICT(source_project, namespace_name, pod_uid, pod_name) DO UPDATE SET
              cluster_name = excluded.cluster_name,
              location = excluded.location,
              build_system = excluded.build_system,
              pod_labels_json = excluded.pod_labels_json,
              pod_annotations_json = excluded.pod_annotations_json,
              metadata_observed_at = excluded.metadata_observed_at,
              pod_author = excluded.pod_author,
              pod_org = excluded.pod_org,
              pod_repo = excluded.pod_repo,
              jenkins_label = excluded.jenkins_label,
              jenkins_label_digest = excluded.jenkins_label_digest,
              jenkins_controller = excluded.jenkins_controller,
              ci_job = excluded.ci_job,
              jenkins_build_url_key = excluded.jenkins_build_url_key,
              source_prow_job_id = excluded.source_prow_job_id,
              normalized_build_key = excluded.normalized_build_key,
              repo_full_name = excluded.repo_full_name,
              job_name = excluded.job_name,
              scheduled_at = excluded.scheduled_at,
              first_pulling_at = excluded.first_pulling_at,
              first_pulled_at = excluded.first_pulled_at,
              first_created_at = excluded.first_created_at,
              first_started_at = excluded.first_started_at,
              last_failed_scheduling_at = excluded.last_failed_scheduling_at,
              failed_scheduling_count = excluded.failed_scheduling_count,
              last_event_at = excluded.last_event_at,
              schedule_to_started_seconds = excluded.schedule_to_started_seconds,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO ci_l1_pod_lifecycle (
          source_project, cluster_name, location, namespace_name, pod_name, pod_uid,
          build_system, pod_labels_json, pod_annotations_json, metadata_observed_at,
          pod_author, pod_org, pod_repo, jenkins_label, jenkins_label_digest, jenkins_controller, ci_job,
          jenkins_build_url_key, source_prow_job_id, normalized_build_key, repo_full_name, job_name,
          scheduled_at, first_pulling_at, first_pulled_at, first_created_at, first_started_at,
          last_failed_scheduling_at, failed_scheduling_count, last_event_at, schedule_to_started_seconds
        ) VALUES (
          :source_project, :cluster_name, :location, :namespace_name, :pod_name, :pod_uid,
          :build_system, :pod_labels_json, :pod_annotations_json, :metadata_observed_at,
          :pod_author, :pod_org, :pod_repo, :jenkins_label, :jenkins_label_digest, :jenkins_controller, :ci_job,
          :jenkins_build_url_key, :source_prow_job_id, :normalized_build_key, :repo_full_name, :job_name,
          :scheduled_at, :first_pulling_at, :first_pulled_at, :first_created_at, :first_started_at,
          :last_failed_scheduling_at, :failed_scheduling_count, :last_event_at, :schedule_to_started_seconds
        )
        ON DUPLICATE KEY UPDATE
          cluster_name = VALUES(cluster_name),
          location = VALUES(location),
          build_system = VALUES(build_system),
          pod_labels_json = VALUES(pod_labels_json),
          pod_annotations_json = VALUES(pod_annotations_json),
          metadata_observed_at = VALUES(metadata_observed_at),
          pod_author = VALUES(pod_author),
          pod_org = VALUES(pod_org),
          pod_repo = VALUES(pod_repo),
          jenkins_label = VALUES(jenkins_label),
          jenkins_label_digest = VALUES(jenkins_label_digest),
          jenkins_controller = VALUES(jenkins_controller),
          ci_job = VALUES(ci_job),
          jenkins_build_url_key = VALUES(jenkins_build_url_key),
          source_prow_job_id = VALUES(source_prow_job_id),
          normalized_build_key = VALUES(normalized_build_key),
          repo_full_name = VALUES(repo_full_name),
          job_name = VALUES(job_name),
          scheduled_at = VALUES(scheduled_at),
          first_pulling_at = VALUES(first_pulling_at),
          first_pulled_at = VALUES(first_pulled_at),
          first_created_at = VALUES(first_created_at),
          first_started_at = VALUES(first_started_at),
          last_failed_scheduling_at = VALUES(last_failed_scheduling_at),
          failed_scheduling_count = VALUES(failed_scheduling_count),
          last_event_at = VALUES(last_event_at),
          schedule_to_started_seconds = VALUES(schedule_to_started_seconds),
          updated_at = CURRENT_TIMESTAMP
        """
    )
