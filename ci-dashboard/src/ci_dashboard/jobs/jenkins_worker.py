from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import ConsumeJenkinsEventsSummary
from ci_dashboard.jobs.build_merge import fetch_existing_build_targets, resolve_merge_target_id
from ci_dashboard.jobs.build_url_matcher import (
    classify_build_system,
    classify_cloud_phase,
    normalize_build_url,
    normalized_job_path_from_key,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "ci-consume-jenkins-events"

AUDIT_STATUS_RECEIVED = "RECEIVED"
AUDIT_STATUS_PROCESSED = "PROCESSED"
AUDIT_STATUS_FAILED = "FAILED"

SELECT_AUDIT_STATUS = text(
    """
    SELECT processing_status
    FROM ci_l1_jenkins_build_events
    WHERE event_id = :event_id
    """
)

INSERT_AUDIT_ROW = text(
    """
    INSERT INTO ci_l1_jenkins_build_events (
      event_id,
      event_type,
      event_time,
      received_at,
      normalized_build_url,
      build_url,
      result,
      payload_json,
      processing_status,
      last_error
    ) VALUES (
      :event_id,
      :event_type,
      :event_time,
      :received_at,
      :normalized_build_url,
      :build_url,
      :result,
      :payload_json,
      :processing_status,
      :last_error
    )
    """
)

MARK_AUDIT_PROCESSED = text(
    """
    UPDATE ci_l1_jenkins_build_events
    SET event_type = :event_type,
        event_time = :event_time,
        normalized_build_url = :normalized_build_url,
        build_url = :build_url,
        result = :result,
        payload_json = :payload_json,
        processing_status = :processing_status,
        last_error = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE event_id = :event_id
    """
)

MARK_AUDIT_FAILED = text(
    """
    UPDATE ci_l1_jenkins_build_events
    SET event_type = :event_type,
        event_time = :event_time,
        normalized_build_url = :normalized_build_url,
        build_url = :build_url,
        result = :result,
        payload_json = :payload_json,
        processing_status = :processing_status,
        last_error = :last_error,
        updated_at = CURRENT_TIMESTAMP
    WHERE event_id = :event_id
    """
)

INSERT_BUILD_FROM_JENKINS = text(
    """
    INSERT INTO ci_l1_builds (
      source_prow_row_id,
      source_prow_job_id,
      namespace,
      job_name,
      job_type,
      state,
      optional,
      report,
      org,
      repo,
      repo_full_name,
      base_ref,
      pr_number,
      is_pr_build,
      context,
      url,
      normalized_build_url,
      author,
      retest,
      event_guid,
      build_id,
      pod_name,
      pending_time,
      start_time,
      completion_time,
      queue_wait_seconds,
      run_seconds,
      total_seconds,
      head_sha,
      target_branch,
      cloud_phase,
      build_system
    ) VALUES (
      :source_prow_row_id,
      :source_prow_job_id,
      :namespace,
      :job_name,
      :job_type,
      :state,
      :optional,
      :report,
      :org,
      :repo,
      :repo_full_name,
      :base_ref,
      :pr_number,
      :is_pr_build,
      :context,
      :url,
      :normalized_build_url,
      :author,
      :retest,
      :event_guid,
      :build_id,
      :pod_name,
      :pending_time,
      :start_time,
      :completion_time,
      :queue_wait_seconds,
      :run_seconds,
      :total_seconds,
      :head_sha,
      :target_branch,
      :cloud_phase,
      :build_system
    )
    """
)

UPDATE_BUILD_FROM_JENKINS = text(
    """
    UPDATE ci_l1_builds
    SET state = :state,
        url = COALESCE(url, :url),
        normalized_build_url = COALESCE(normalized_build_url, :normalized_build_url),
        job_name = COALESCE(job_name, :job_name),
        job_type = COALESCE(job_type, :job_type),
        org = COALESCE(org, :org),
        repo = COALESCE(repo, :repo),
        repo_full_name = COALESCE(repo_full_name, :repo_full_name),
        base_ref = COALESCE(base_ref, :base_ref),
        pr_number = COALESCE(pr_number, :pr_number),
        is_pr_build = CASE
          WHEN is_pr_build = 0 AND :is_pr_build = 1 THEN 1
          ELSE is_pr_build
        END,
        context = COALESCE(context, :context),
        author = COALESCE(author, :author),
        build_id = COALESCE(build_id, :build_id),
        start_time = COALESCE(start_time, :start_time),
        completion_time = COALESCE(completion_time, :completion_time),
        run_seconds = COALESCE(run_seconds, :run_seconds),
        total_seconds = COALESCE(total_seconds, :total_seconds),
        head_sha = COALESCE(head_sha, :head_sha),
        target_branch = COALESCE(target_branch, :target_branch),
        cloud_phase = CASE
          WHEN cloud_phase = 'IDC' AND :cloud_phase = 'GCP' THEN :cloud_phase
          ELSE cloud_phase
        END,
        build_system = CASE
          WHEN build_system = 'UNKNOWN' THEN :build_system
          ELSE build_system
        END,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :id
    """
)


@dataclass(frozen=True)
class CloudEventEnvelope:
    event_id: str
    event_type: str | None
    event_time: datetime | None


@dataclass(frozen=True)
class ParsedJenkinsFinishedEvent:
    event_id: str
    event_type: str
    event_time: datetime | None
    build_url: str
    normalized_build_url: str
    jenkins_result: str | None
    state: str
    job_name: str | None
    job_type: str | None
    org: str | None
    repo: str | None
    repo_full_name: str | None
    base_ref: str | None
    pr_number: int | None
    is_pr_build: bool
    context: str | None
    author: str | None
    build_id: str | None
    start_time: datetime | None
    completion_time: datetime | None
    run_seconds: int | None
    total_seconds: int | None
    head_sha: str | None
    target_branch: str | None
    cloud_phase: str
    build_system: str

    def as_build_row_params(self) -> dict[str, Any]:
        return {
            "id": None,
            "source_prow_row_id": None,
            "source_prow_job_id": None,
            "namespace": None,
            "job_name": self.job_name,
            "job_type": self.job_type,
            "state": self.state,
            "optional": 0,
            "report": 0,
            "org": self.org,
            "repo": self.repo,
            "repo_full_name": self.repo_full_name,
            "base_ref": self.base_ref,
            "pr_number": self.pr_number,
            "is_pr_build": int(self.is_pr_build),
            "context": self.context,
            "url": self.build_url,
            "normalized_build_url": self.normalized_build_url,
            "author": self.author,
            "retest": None,
            "event_guid": None,
            "build_id": self.build_id,
            "pod_name": None,
            "pending_time": None,
            "start_time": self.start_time,
            "completion_time": self.completion_time,
            "queue_wait_seconds": None,
            "run_seconds": self.run_seconds,
            "total_seconds": self.total_seconds,
            "head_sha": self.head_sha,
            "target_branch": self.target_branch,
            "cloud_phase": self.cloud_phase,
            "build_system": self.build_system,
        }

    def as_audit_params(self, *, payload_json: str, status: str, last_error: str | None = None) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_time": self.event_time,
            "normalized_build_url": self.normalized_build_url,
            "build_url": self.build_url,
            "result": self.jenkins_result,
            "payload_json": payload_json,
            "processing_status": status,
            "last_error": last_error,
        }


def run_consume_jenkins_events(
    engine: Engine,
    settings: Settings,
    *,
    max_messages: int | None = None,
    topic: str | None = None,
    group_id: str | None = None,
    consumer: Any | None = None,
) -> ConsumeJenkinsEventsSummary:
    summary = ConsumeJenkinsEventsSummary()
    resolved_topic = topic or settings.kafka.jenkins_events_topic
    resolved_group_id = group_id or settings.kafka.jenkins_consumer_group
    resolved_consumer = consumer or _build_kafka_consumer(
        settings,
        topic=resolved_topic,
        group_id=resolved_group_id,
    )
    should_close_consumer = consumer is None

    try:
        while max_messages is None or summary.messages_polled < max_messages:
            remaining = None if max_messages is None else max_messages - summary.messages_polled
            records_by_partition = resolved_consumer.poll(
                timeout_ms=settings.kafka.poll_timeout_ms,
                max_records=1 if remaining is None else max(1, min(remaining, 1)),
            )
            if not records_by_partition:
                if max_messages is not None:
                    break
                continue

            for records in records_by_partition.values():
                for record in records:
                    if max_messages is not None and summary.messages_polled >= max_messages:
                        return summary

                    summary.messages_polled += 1
                    try:
                        result = process_jenkins_event_message(engine, settings, record.value)
                    except Exception:
                        summary.events_failed += 1
                        LOG.exception(
                            "failed to process Jenkins finished event",
                            extra={"job_name": JOB_NAME},
                        )
                        continue

                    if result == "skipped":
                        summary.events_skipped += 1
                    elif result == "failed":
                        summary.events_failed += 1
                    else:
                        summary.events_processed += 1
                        summary.build_rows_written += 1
                    resolved_consumer.commit()
    finally:
        if should_close_consumer and hasattr(resolved_consumer, "close"):
            resolved_consumer.close()

    return summary


def process_jenkins_event_message(engine: Engine, settings: Settings, raw_value: bytes | str | Mapping[str, Any]) -> str:
    payload = _decode_cloud_event_payload(raw_value)
    envelope = _extract_cloud_event_envelope(payload)
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    parsed: ParsedJenkinsFinishedEvent | None = None

    with engine.begin() as connection:
        current_status = _ensure_audit_row(
            connection,
            envelope=envelope,
            payload_json=payload_json,
        )
        if current_status == AUDIT_STATUS_PROCESSED:
            return "skipped"

    try:
        parsed = parse_jenkins_finished_event(payload, settings)
        with engine.begin() as connection:
            _upsert_build_from_jenkins_event(connection, parsed)
            connection.execute(
                MARK_AUDIT_PROCESSED,
                parsed.as_audit_params(
                    payload_json=payload_json,
                    status=AUDIT_STATUS_PROCESSED,
                ),
            )
        return "processed"
    except Exception as exc:
        with engine.begin() as connection:
            connection.execute(
                MARK_AUDIT_FAILED,
                {
                    "event_id": envelope.event_id,
                    "event_type": parsed.event_type if parsed else envelope.event_type,
                    "event_time": parsed.event_time if parsed else envelope.event_time,
                    "normalized_build_url": parsed.normalized_build_url if parsed else None,
                    "build_url": parsed.build_url if parsed else None,
                    "result": parsed.jenkins_result if parsed else None,
                    "payload_json": payload_json,
                    "processing_status": AUDIT_STATUS_FAILED,
                    "last_error": str(exc),
                },
            )
        return "failed"


def parse_jenkins_finished_event(
    payload: Mapping[str, Any],
    settings: Settings,
) -> ParsedJenkinsFinishedEvent:
    envelope = _extract_cloud_event_envelope(payload)
    if envelope.event_type != settings.jenkins_ingest.finished_event_type:
        raise ValueError(
            f"unsupported CloudEvent type: {envelope.event_type!r}, expected {settings.jenkins_ingest.finished_event_type!r}"
        )

    data = _as_mapping(payload.get("data"))
    custom_data = _as_mapping(
        _first_non_none(
            data.get("customData"),
            data.get("custom_data"),
            data.get("params"),
            data.get("parameters"),
        )
    )

    build_url = _extract_build_url(payload, data, custom_data)
    if build_url is None:
        raise ValueError("missing Jenkins build URL in finished event")

    normalized_build_url = normalize_build_url(build_url)
    if normalized_build_url is None:
        raise ValueError(f"failed to normalize Jenkins build URL: {build_url!r}")

    build_url_org, build_url_repo = _extract_repo_from_build_url(normalized_build_url)
    job_name = _first_non_empty_str(
        custom_data.get("job_name"),
        custom_data.get("jobName"),
        custom_data.get("pipelineName"),
        _extract_job_name_from_build_url(normalized_build_url),
    )

    jenkins_result = _extract_result(data, custom_data)
    state = map_jenkins_result_to_state(jenkins_result)

    org = _first_non_empty_str(
        custom_data.get("org"),
        custom_data.get("github_org"),
        custom_data.get("ghprbGhRepositoryOwner"),
        build_url_org,
    )
    repo_candidate = _first_non_empty_str(
        custom_data.get("repo"),
        custom_data.get("github_repo"),
        custom_data.get("ghprbGhRepositoryName"),
        custom_data.get("repository"),
        build_url_repo,
    )
    repo_full_name = _first_non_empty_str(
        custom_data.get("repo_full_name"),
        custom_data.get("github_repo_full_name"),
        custom_data.get("ghprbGhRepository"),
    )
    org, repo, repo_full_name = _normalize_repo_fields(org, repo_candidate, repo_full_name)
    if repo_full_name is None and org and repo:
        repo_full_name = f"{org}/{repo}"

    target_branch = _first_non_empty_str(
        custom_data.get("target_branch"),
        custom_data.get("branch"),
        custom_data.get("ghprbTargetBranch"),
    )
    pr_number = _first_non_empty_int(
        custom_data.get("pr_number"),
        custom_data.get("pr"),
        custom_data.get("pull"),
        custom_data.get("ghprbPullId"),
    )
    start_time = _parse_datetime(
        _first_non_none(
            data.get("startTime"),
            data.get("start_time"),
            data.get("startedAt"),
            custom_data.get("startTime"),
            custom_data.get("start_time"),
            custom_data.get("startedAt"),
        )
    )
    completion_time = _parse_datetime(
        _first_non_none(
            data.get("completionTime"),
            data.get("completion_time"),
            data.get("endTime"),
            data.get("finishedAt"),
            custom_data.get("completionTime"),
            custom_data.get("completion_time"),
            custom_data.get("endTime"),
            custom_data.get("finishedAt"),
            envelope.event_time,
        )
    )
    total_seconds = _duration_seconds(start_time, completion_time)
    build_id = _first_non_empty_str(
        custom_data.get("build_id"),
        custom_data.get("buildId"),
        _extract_build_number_from_url(normalized_build_url),
    )
    return ParsedJenkinsFinishedEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type or settings.jenkins_ingest.finished_event_type,
        event_time=envelope.event_time,
        build_url=build_url,
        normalized_build_url=normalized_build_url,
        jenkins_result=jenkins_result,
        state=state,
        job_name=job_name,
        job_type=None,
        org=org,
        repo=repo,
        repo_full_name=repo_full_name,
        base_ref=target_branch,
        pr_number=pr_number,
        is_pr_build=pr_number is not None,
        context=_first_non_empty_str(custom_data.get("context")),
        author=_first_non_empty_str(custom_data.get("author"), custom_data.get("triggered_by")),
        build_id=build_id,
        start_time=start_time,
        completion_time=completion_time,
        run_seconds=total_seconds,
        total_seconds=total_seconds,
        head_sha=_first_non_empty_str(
            custom_data.get("head_sha"),
            custom_data.get("sha"),
            custom_data.get("commit"),
            custom_data.get("ghprbActualCommit"),
            custom_data.get("ghprbPullActualCommit"),
        ),
        target_branch=target_branch,
        cloud_phase=classify_cloud_phase(build_url),
        build_system=classify_build_system(build_url),
    )


def map_jenkins_result_to_state(result: str | None) -> str:
    normalized = str(result or "").strip().upper()
    if normalized == "SUCCESS":
        return "success"
    if normalized in {"FAILURE", "UNSTABLE"}:
        return "failure"
    if normalized == "ABORTED":
        return "aborted"
    if normalized == "NOT_BUILT":
        return "error"
    return "error"


def _upsert_build_from_jenkins_event(connection: Connection, parsed: ParsedJenkinsFinishedEvent) -> None:
    build_params = parsed.as_build_row_params()
    existing_by_prow_job_id, existing_by_build_url = fetch_existing_build_targets(
        connection,
        normalized_build_urls=[parsed.normalized_build_url],
        source_prow_job_ids=[],
    )
    target_id = resolve_merge_target_id(
        normalized_build_url=parsed.normalized_build_url,
        source_prow_job_id=None,
        existing_by_prow_job_id=existing_by_prow_job_id,
        existing_by_build_url=existing_by_build_url,
        log_context={"job_name": JOB_NAME},
    )
    if target_id is None:
        connection.execute(INSERT_BUILD_FROM_JENKINS, build_params)
        return

    build_params["id"] = target_id
    connection.execute(UPDATE_BUILD_FROM_JENKINS, build_params)


def _ensure_audit_row(connection: Connection, *, envelope: CloudEventEnvelope, payload_json: str) -> str | None:
    existing_status = connection.execute(
        SELECT_AUDIT_STATUS,
        {"event_id": envelope.event_id},
    ).scalar_one_or_none()
    if existing_status is not None:
        return str(existing_status)
    connection.execute(
        INSERT_AUDIT_ROW,
        {
            "event_id": envelope.event_id,
            "event_type": envelope.event_type,
            "event_time": envelope.event_time,
            "received_at": _utcnow_naive(),
            "normalized_build_url": None,
            "build_url": None,
            "result": None,
            "payload_json": payload_json,
            "processing_status": AUDIT_STATUS_RECEIVED,
            "last_error": None,
        },
    )
    return None


def _build_kafka_consumer(settings: Settings, *, topic: str, group_id: str) -> Any:
    if not settings.kafka.bootstrap_servers:
        raise ValueError("CI_DASHBOARD_KAFKA_BOOTSTRAP_SERVERS is required for consume-jenkins-events")
    try:
        from kafka import KafkaConsumer
    except ImportError as exc:
        raise RuntimeError(
            "kafka-python-ng is required to consume Jenkins events; reinstall ci-dashboard dependencies"
        ) from exc

    return KafkaConsumer(
        topic,
        bootstrap_servers=list(settings.kafka.bootstrap_servers),
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda value: value,
        key_deserializer=lambda value: value,
    )


def _decode_cloud_event_payload(raw_value: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw_value, Mapping):
        return dict(raw_value)
    if isinstance(raw_value, bytes):
        decoded = raw_value.decode("utf-8")
    else:
        decoded = str(raw_value)
    parsed = json.loads(decoded)
    if not isinstance(parsed, dict):
        raise ValueError("Kafka message must decode to a JSON object")
    return parsed


def _extract_cloud_event_envelope(payload: Mapping[str, Any]) -> CloudEventEnvelope:
    event_id = _first_non_empty_str(payload.get("id"))
    if event_id is None:
        raise ValueError("CloudEvent id is required")
    return CloudEventEnvelope(
        event_id=event_id,
        event_type=_first_non_empty_str(payload.get("type")),
        event_time=_parse_datetime(payload.get("time")),
    )


def _extract_build_url(
    payload: Mapping[str, Any],
    data: Mapping[str, Any],
    custom_data: Mapping[str, Any],
) -> str | None:
    for candidate in (
        payload.get("subject"),
        data.get("url"),
        data.get("buildUrl"),
        data.get("buildURL"),
        data.get("runURL"),
        data.get("runUrl"),
        custom_data.get("url"),
        custom_data.get("buildUrl"),
        custom_data.get("buildURL"),
        custom_data.get("runURL"),
        custom_data.get("runUrl"),
        _nested_get(data, "pipelineRun", "url"),
        _nested_get(data, "pipelineRun", "runURL"),
        _nested_get(data, "pipelineRun", "buildUrl"),
        _nested_get(data, "pipelinerun", "url"),
    ):
        url = _first_non_empty_str(candidate)
        if url and "job/" in url:
            return url
    return None


def _extract_result(data: Mapping[str, Any], custom_data: Mapping[str, Any]) -> str | None:
    return _first_non_empty_str(
        data.get("result"),
        data.get("outcome"),
        data.get("status"),
        custom_data.get("result"),
        custom_data.get("outcome"),
        custom_data.get("status"),
        _nested_get(data, "pipelineRun", "result"),
    )


def _extract_repo_from_build_url(normalized_build_url: str) -> tuple[str | None, str | None]:
    parts = _normalized_build_url_parts(normalized_build_url)
    if len(parts) < 8:
        return None, None
    if not (
        parts[0] == "jenkins"
        and parts[1] == "job"
        and parts[3] == "job"
        and parts[5] == "job"
    ):
        return None, None
    return parts[2], parts[4]


def _extract_job_name_from_build_url(normalized_build_url: str) -> str | None:
    normalized_job_path = normalized_job_path_from_key(normalized_build_url)
    if normalized_job_path is None:
        return None
    parts = _normalized_build_url_parts(normalized_job_path)
    if not parts:
        return None
    return parts[-1]


def _extract_build_number_from_url(normalized_build_url: str) -> str | None:
    parts = _normalized_build_url_parts(normalized_build_url)
    if not parts:
        return None
    return parts[-1]


def _normalized_build_url_parts(normalized_build_url: str) -> list[str]:
    path = urlsplit(normalized_build_url).path or normalized_build_url
    return [part for part in path.strip("/").split("/") if part]


def _normalize_repo_fields(
    org: str | None,
    repo_candidate: str | None,
    repo_full_name: str | None,
) -> tuple[str | None, str | None, str | None]:
    if repo_full_name and "/" in repo_full_name:
        repo_org, repo_name = repo_full_name.split("/", 1)
        return org or repo_org, repo_candidate or repo_name, repo_full_name
    if repo_candidate and "/" in repo_candidate:
        repo_org, repo_name = repo_candidate.split("/", 1)
        return org or repo_org, repo_name, f"{org or repo_org}/{repo_name}"
    if org and repo_candidate:
        return org, repo_candidate, f"{org}/{repo_candidate}"
    return org, repo_candidate, repo_full_name


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _nested_get(mapping: Mapping[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _first_non_empty_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _first_non_empty_int(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        normalized = str(value).strip()
        if not normalized:
            continue
        return int(normalized)
    return None


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


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
