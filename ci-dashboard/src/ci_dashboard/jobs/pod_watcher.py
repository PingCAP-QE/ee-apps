from __future__ import annotations

import json
import logging
import os
import queue
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import WatchPodsSummary
from ci_dashboard.common.sql_helpers import chunked
from ci_dashboard.jobs.build_url_matcher import canonicalize_job_name
from ci_dashboard.jobs.state_store import mark_job_failed, mark_job_started, mark_job_succeeded
from ci_dashboard.jobs.sync_pods import (
    POD_EVENT_REASONS,
    NormalizedPodEvent,
    PodMetadataSnapshot,
    _build_lifecycle_rows,
    _build_requested_pods_relation,
    _coerce_str,
    _coerce_str_mapping,
    _decode_json_object,
    _get_json,
    _get_kubernetes_api_ca_file,
    _get_kubernetes_api_token,
    _get_kubernetes_api_url,
    _infer_pod_build_system,
    _json_loads_str_mapping,
    _load_build_metadata_map,
    _load_jenkins_pod_name_url_prefix_map,
    _load_target_namespaces,
    _null_safe_equals_sql,
    _parse_datetime,
    _pod_identity_from_values,
    _read_int_env,
    _supplement_jenkins_pod_fields_from_build_metadata,
    _upsert_pod_events,
    _upsert_pod_lifecycle,
)

JOB_NAME = "ci-watch-pods"
WATCH_USER_AGENT = "ci-dashboard-watch-pods"
DEFAULT_WATCH_TIMEOUT_SECONDS = 300
DEFAULT_WATCH_RETRY_DELAY_SECONDS = 5
DEFAULT_HEALTH_PORT = 8081
DEFAULT_HEALTH_STALE_AFTER_SECONDS = 720
DEFAULT_JENKINS_PREFIX_CACHE_SECONDS = 900
DEFAULT_DB_BATCH_SIZE = 100
DEFAULT_DB_RETRY_ATTEMPTS = 3
DEFAULT_DB_RETRY_BASE_DELAY_MS = 500
DEFAULT_DB_RETRY_MAX_DELAY_MS = 5000
RETRYABLE_MYSQL_ERROR_CODES = frozenset({1205, 1213})
POD_WATCH_TYPES = frozenset({"ADDED", "MODIFIED"})
EVENT_WATCH_TYPES = frozenset({"ADDED", "MODIFIED"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchRuntimeContext:
    source_project: str
    cluster_name: str | None
    location: str | None


@dataclass(frozen=True)
class WatchedPodSnapshot:
    source_project: str
    cluster_name: str | None
    location: str | None
    namespace_name: str
    pod_name: str
    pod_uid: str
    snapshot: PodMetadataSnapshot

    @property
    def identity(self) -> tuple[str, str | None, str | None, str | None]:
        return _pod_identity_from_values(
            source_project=self.source_project,
            namespace_name=self.namespace_name,
            pod_uid=self.pod_uid,
            pod_name=self.pod_name,
        )


class WatchHealthState:
    def __init__(self, *, stale_after_seconds: int) -> None:
        self._stale_after_seconds = stale_after_seconds
        self._streams: dict[str, float | None] = {}
        self._lock = threading.Lock()

    def register(self, stream_key: str) -> None:
        with self._lock:
            self._streams.setdefault(stream_key, None)

    def heartbeat(self, stream_key: str) -> None:
        with self._lock:
            self._streams[stream_key] = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            streams = {
                key: {
                    "age_seconds": None if value is None else round(now - value, 3),
                    "healthy": value is not None and now - value <= self._stale_after_seconds,
                }
                for key, value in sorted(self._streams.items())
            }
        healthy = bool(streams) and all(item["healthy"] for item in streams.values())
        return {
            "healthy": healthy,
            "stale_after_seconds": self._stale_after_seconds,
            "streams": streams,
        }


class _ProgressRecorder:
    def __init__(
        self,
        summary: WatchPodsSummary,
        *,
        max_events: int | None,
        stop_event: threading.Event,
    ) -> None:
        self._summary = summary
        self._max_events = max_events
        self._stop_event = stop_event
        self._lock = threading.Lock()

    def add(
        self,
        *,
        pod_snapshots_seen: int = 0,
        event_rows_seen: int = 0,
        event_rows_written: int = 0,
        lifecycle_rows_upserted: int = 0,
        pods_touched: int = 0,
        watch_restarts: int = 0,
    ) -> None:
        with self._lock:
            self._summary.pod_snapshots_seen += pod_snapshots_seen
            self._summary.event_rows_seen += event_rows_seen
            self._summary.event_rows_written += event_rows_written
            self._summary.lifecycle_rows_upserted += lifecycle_rows_upserted
            self._summary.pods_touched += pods_touched
            self._summary.watch_restarts += watch_restarts
            processed = self._summary.pod_snapshots_seen + self._summary.event_rows_seen
            if self._max_events is not None and processed >= self._max_events:
                self._stop_event.set()


class _JenkinsPodNameUrlPrefixCache:
    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._prefixes: dict[str, str] | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self, connection: Connection) -> dict[str, str]:
        now = time.monotonic()
        with self._lock:
            if self._prefixes is not None and (
                self._ttl_seconds == 0 or now < self._expires_at
            ):
                return self._prefixes
            prefixes = _load_jenkins_pod_name_url_prefix_map(connection)
            self._prefixes = prefixes
            self._expires_at = float("inf") if self._ttl_seconds == 0 else now + self._ttl_seconds
            return prefixes


class _KubernetesClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        ca_file: str | None,
        watch_timeout_seconds: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._ca_file = ca_file
        self._watch_timeout_seconds = watch_timeout_seconds

    def list_collection(self, *, namespace_name: str, resource: str) -> tuple[list[dict[str, Any]], str | None]:
        items: list[dict[str, Any]] = []
        continue_token: str | None = None
        resource_version: str | None = None
        while True:
            query = {"limit": "500"}
            if continue_token:
                query["continue"] = continue_token
            response = _get_json(
                self._collection_url(namespace_name=namespace_name, resource=resource, query=query),
                headers={"Authorization": f"Bearer {self._token}"},
                ca_file=self._ca_file,
            )
            raw_items = response.get("items")
            if isinstance(raw_items, list):
                items.extend(item for item in raw_items if isinstance(item, dict))

            metadata = response.get("metadata")
            continue_token = None
            if isinstance(metadata, dict):
                continue_token = _coerce_str(metadata.get("continue"))
                resource_version = _coerce_str(metadata.get("resourceVersion")) or resource_version
            if continue_token is None:
                return items, resource_version

    def stream_collection(
        self,
        *,
        namespace_name: str,
        resource: str,
        resource_version: str | None,
    ) -> Iterable[dict[str, Any]]:
        query = {
            "watch": "true",
            "allowWatchBookmarks": "true",
            "timeoutSeconds": str(self._watch_timeout_seconds),
        }
        if resource_version:
            query["resourceVersion"] = resource_version
        url = self._collection_url(namespace_name=namespace_name, resource=resource, query=query)
        yield from _stream_watch_json(
            url,
            token=self._token,
            ca_file=self._ca_file,
            timeout_seconds=self._watch_timeout_seconds + 30,
        )

    def _collection_url(self, *, namespace_name: str, resource: str, query: dict[str, str]) -> str:
        namespace = urllib_parse.quote(namespace_name, safe="")
        encoded_query = urllib_parse.urlencode(query)
        return f"{self._base_url}/api/v1/namespaces/{namespace}/{resource}?{encoded_query}"


def run_watch_pods(
    engine: Engine,
    settings: Settings,
    *,
    max_events: int | None = None,
) -> WatchPodsSummary:
    if max_events is not None and max_events <= 0:
        raise ValueError("max_events must be positive")

    summary = WatchPodsSummary()
    stop_event = threading.Event()
    recorder = _ProgressRecorder(summary, max_events=max_events, stop_event=stop_event)
    worker_errors: queue.Queue[BaseException] = queue.Queue()
    context = _load_runtime_context()
    jenkins_prefix_cache = _JenkinsPodNameUrlPrefixCache(
        ttl_seconds=_read_non_negative_int_env(
            "CI_DASHBOARD_JENKINS_POD_NAME_PREFIX_CACHE_SECONDS",
            DEFAULT_JENKINS_PREFIX_CACHE_SECONDS,
        )
    )
    watch_timeout_seconds = _read_int_env(
        "CI_DASHBOARD_POD_WATCH_TIMEOUT_SECONDS",
        DEFAULT_WATCH_TIMEOUT_SECONDS,
    )
    health_state = WatchHealthState(
        stale_after_seconds=_read_int_env(
            "CI_DASHBOARD_POD_WATCH_STALE_AFTER_SECONDS",
            max(DEFAULT_HEALTH_STALE_AFTER_SECONDS, watch_timeout_seconds * 2 + 120),
        )
    )
    client = _KubernetesClient(
        base_url=_get_kubernetes_api_url(),
        token=_get_kubernetes_api_token(),
        ca_file=_get_kubernetes_api_ca_file(),
        watch_timeout_seconds=watch_timeout_seconds,
    )

    with engine.begin() as connection:
        namespaces = _load_target_namespaces(connection)
        watermark = {
            "namespaces": namespaces,
            "source_project": context.source_project,
        }
        mark_job_started(connection, JOB_NAME, watermark)

    threads: list[threading.Thread] = []
    for namespace_name in namespaces:
        pod_stream_key = _stream_key(namespace_name, "pods")
        event_stream_key = _stream_key(namespace_name, "events")
        health_state.register(pod_stream_key)
        health_state.register(event_stream_key)
        threads.append(
            threading.Thread(
                target=_worker_guard,
                name=f"pod-watch:{namespace_name}:pods",
                kwargs={
                    "worker_errors": worker_errors,
                    "stop_event": stop_event,
                    "target": _run_pod_worker,
                    "target_kwargs": {
                        "engine": engine,
                        "settings": settings,
                        "client": client,
                        "context": context,
                        "namespace_name": namespace_name,
                        "stream_key": pod_stream_key,
                        "health_state": health_state,
                        "recorder": recorder,
                        "jenkins_prefix_cache": jenkins_prefix_cache,
                        "stop_event": stop_event,
                    },
                },
                daemon=True,
            )
        )
        threads.append(
            threading.Thread(
                target=_worker_guard,
                name=f"pod-watch:{namespace_name}:events",
                kwargs={
                    "worker_errors": worker_errors,
                    "stop_event": stop_event,
                    "target": _run_event_worker,
                    "target_kwargs": {
                        "engine": engine,
                        "settings": settings,
                        "client": client,
                        "context": context,
                        "namespace_name": namespace_name,
                        "stream_key": event_stream_key,
                        "health_state": health_state,
                        "recorder": recorder,
                        "jenkins_prefix_cache": jenkins_prefix_cache,
                        "stop_event": stop_event,
                    },
                },
                daemon=True,
            )
        )

    health_server = _start_health_server(health_state)
    for thread in threads:
        thread.start()

    try:
        while any(thread.is_alive() for thread in threads):
            if max_events is not None and stop_event.is_set():
                break
            try:
                raise worker_errors.get(timeout=1)
            except queue.Empty:
                continue

        if not worker_errors.empty():
            raise worker_errors.get()

        with engine.begin() as connection:
            mark_job_succeeded(connection, JOB_NAME, {"namespaces": namespaces})
        return summary
    except BaseException as exc:
        stop_event.set()
        with engine.begin() as connection:
            mark_job_failed(connection, JOB_NAME, {"namespaces": namespaces}, str(exc))
        raise
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("pod watcher thread did not exit cleanly: %s", thread.name)
        if health_server is not None:
            health_server.shutdown()
            health_server.server_close()


def _worker_guard(
    *,
    worker_errors: queue.Queue[BaseException],
    stop_event: threading.Event,
    target,
    target_kwargs: dict[str, Any],
) -> None:
    try:
        target(**target_kwargs)
    except BaseException as exc:
        worker_errors.put(exc)
        stop_event.set()


def _run_pod_worker(
    *,
    engine: Engine,
    settings: Settings,
    client: _KubernetesClient,
    context: WatchRuntimeContext,
    namespace_name: str,
    stream_key: str,
    health_state: WatchHealthState,
    recorder: _ProgressRecorder,
    jenkins_prefix_cache: _JenkinsPodNameUrlPrefixCache,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            items, resource_version = client.list_collection(
                namespace_name=namespace_name,
                resource="pods",
            )
            health_state.heartbeat(stream_key)
            snapshots = [
                snapshot
                for snapshot in (
                    _normalize_pod_object(item, context=context) for item in items
                )
                if snapshot is not None
            ]
            lifecycle_count = _persist_pod_snapshots(
                engine,
                settings,
                snapshots,
                jenkins_prefix_cache=jenkins_prefix_cache,
                on_batch_persisted=lambda: health_state.heartbeat(stream_key),
            )
            recorder.add(
                pod_snapshots_seen=len(snapshots),
                lifecycle_rows_upserted=lifecycle_count,
                pods_touched=len(snapshots),
            )

            for watch_event in client.stream_collection(
                namespace_name=namespace_name,
                resource="pods",
                resource_version=resource_version,
            ):
                if stop_event.is_set():
                    break
                health_state.heartbeat(stream_key)
                event_type = _coerce_str(watch_event.get("type"))
                if event_type == "ERROR":
                    if _is_resource_version_expired_watch_error(watch_event):
                        recorder.add(watch_restarts=1)
                        logger.info(
                            "pod watch resource version expired for namespace %s; relisting",
                            namespace_name,
                        )
                        break
                    raise RuntimeError(_format_watch_error(watch_event))
                if event_type not in POD_WATCH_TYPES:
                    continue
                pod_object = watch_event.get("object")
                if not isinstance(pod_object, dict):
                    continue
                snapshot = _normalize_pod_object(pod_object, context=context)
                if snapshot is None:
                    continue
                lifecycle_count = _persist_pod_snapshots(
                    engine,
                    settings,
                    [snapshot],
                    jenkins_prefix_cache=jenkins_prefix_cache,
                    on_batch_persisted=lambda: health_state.heartbeat(stream_key),
                )
                recorder.add(
                    pod_snapshots_seen=1,
                    lifecycle_rows_upserted=lifecycle_count,
                    pods_touched=1,
                )
        except Exception as exc:
            if stop_event.is_set():
                break
            recorder.add(watch_restarts=1)
            logger.warning(
                "pod watch stream restarted for namespace %s: %s",
                namespace_name,
                exc,
            )
            _sleep_until_retry(stop_event)


def _run_event_worker(
    *,
    engine: Engine,
    settings: Settings,
    client: _KubernetesClient,
    context: WatchRuntimeContext,
    namespace_name: str,
    stream_key: str,
    health_state: WatchHealthState,
    recorder: _ProgressRecorder,
    jenkins_prefix_cache: _JenkinsPodNameUrlPrefixCache,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            items, resource_version = client.list_collection(
                namespace_name=namespace_name,
                resource="events",
            )
            health_state.heartbeat(stream_key)
            normalized_rows = [
                row
                for row in (
                    _normalize_kubernetes_event(item, context=context) for item in items
                )
                if row is not None
            ]
            event_count, lifecycle_count, pods_touched = _persist_pod_events(
                engine,
                settings,
                normalized_rows,
                jenkins_prefix_cache=jenkins_prefix_cache,
                on_batch_persisted=lambda: health_state.heartbeat(stream_key),
            )
            recorder.add(
                event_rows_seen=len(normalized_rows),
                event_rows_written=event_count,
                lifecycle_rows_upserted=lifecycle_count,
                pods_touched=pods_touched,
            )

            for watch_event in client.stream_collection(
                namespace_name=namespace_name,
                resource="events",
                resource_version=resource_version,
            ):
                if stop_event.is_set():
                    break
                health_state.heartbeat(stream_key)
                event_type = _coerce_str(watch_event.get("type"))
                if event_type == "ERROR":
                    if _is_resource_version_expired_watch_error(watch_event):
                        recorder.add(watch_restarts=1)
                        logger.info(
                            "event watch resource version expired for namespace %s; relisting",
                            namespace_name,
                        )
                        break
                    raise RuntimeError(_format_watch_error(watch_event))
                if event_type not in EVENT_WATCH_TYPES:
                    continue
                event_object = watch_event.get("object")
                if not isinstance(event_object, dict):
                    continue
                normalized = _normalize_kubernetes_event(event_object, context=context)
                if normalized is None:
                    continue
                event_count, lifecycle_count, pods_touched = _persist_pod_events(
                    engine,
                    settings,
                    [normalized],
                    jenkins_prefix_cache=jenkins_prefix_cache,
                    on_batch_persisted=lambda: health_state.heartbeat(stream_key),
                )
                recorder.add(
                    event_rows_seen=1,
                    event_rows_written=event_count,
                    lifecycle_rows_upserted=lifecycle_count,
                    pods_touched=pods_touched,
                )
        except Exception as exc:
            if stop_event.is_set():
                break
            recorder.add(watch_restarts=1)
            logger.warning(
                "event watch stream restarted for namespace %s: %s",
                namespace_name,
                exc,
            )
            _sleep_until_retry(stop_event)


def _persist_pod_snapshots(
    engine: Engine,
    settings: Settings,
    snapshots: list[WatchedPodSnapshot],
    *,
    jenkins_prefix_cache: _JenkinsPodNameUrlPrefixCache | None = None,
    on_batch_persisted: Callable[[], None] | None = None,
) -> int:
    if not snapshots:
        return 0

    rows_written = 0
    for batch in chunked(snapshots, _db_batch_size(settings)):
        batch = list(batch)
        def _persist_batch(connection: Connection) -> int:
            jenkins_prefixes = _get_jenkins_prefixes_if_needed(
                connection,
                [snapshot.identity for snapshot in batch],
                jenkins_prefix_cache,
            )
            lifecycle_rows = _build_lifecycle_rows_for_snapshots(
                connection,
                batch,
                jenkins_pod_name_url_prefixes=jenkins_prefixes,
            )
            if lifecycle_rows:
                _upsert_pod_lifecycle(connection, lifecycle_rows)
            return len(lifecycle_rows)

        rows_written += _run_db_transaction_with_retry(
            engine,
            _persist_batch,
            on_retry=on_batch_persisted,
        )
        if on_batch_persisted is not None:
            on_batch_persisted()
    return rows_written


def _persist_pod_events(
    engine: Engine,
    settings: Settings,
    rows: list[NormalizedPodEvent],
    *,
    jenkins_prefix_cache: _JenkinsPodNameUrlPrefixCache | None = None,
    on_batch_persisted: Callable[[], None] | None = None,
) -> tuple[int, int, int]:
    if not rows:
        return 0, 0, 0

    event_rows_written = 0
    lifecycle_rows_written = 0
    touched_pods: set[tuple[str, str | None, str | None, str | None]] = set()
    for batch in chunked(rows, _db_batch_size(settings)):
        batch_identities = {
            _pod_identity_from_values(
                source_project=row.source_project,
                namespace_name=row.namespace_name,
                pod_uid=row.pod_uid,
                pod_name=row.pod_name,
            )
            for row in batch
            if row.namespace_name is not None and row.pod_name is not None
        }
        def _persist_batch(connection: Connection) -> int:
            _upsert_pod_events(connection, [row.as_db_params() for row in batch])
            jenkins_prefixes = _get_jenkins_prefixes_if_needed(
                connection,
                batch_identities,
                jenkins_prefix_cache,
            )

            pod_metadata_by_identity = _load_existing_pod_metadata_snapshots(
                connection,
                _sort_identities(batch_identities),
            )
            lifecycle_rows = _build_lifecycle_rows(
                connection,
                _sort_identities(batch_identities),
                pod_metadata_by_identity=pod_metadata_by_identity,
                jenkins_pod_name_url_prefixes=jenkins_prefixes,
            )
            if lifecycle_rows:
                _upsert_pod_lifecycle(connection, lifecycle_rows)
            return len(lifecycle_rows)

        lifecycle_rows_written += _run_db_transaction_with_retry(
            engine,
            _persist_batch,
            on_retry=on_batch_persisted,
        )
        event_rows_written += len(batch)
        if on_batch_persisted is not None:
            on_batch_persisted()
        touched_pods.update(batch_identities)
    return event_rows_written, lifecycle_rows_written, len(touched_pods)


def _db_batch_size(settings: Settings) -> int:
    default = min(settings.jobs.batch_size, DEFAULT_DB_BATCH_SIZE)
    return _read_int_env("CI_DASHBOARD_POD_WATCH_DB_BATCH_SIZE", default)


def _run_db_transaction_with_retry(
    engine: Engine,
    operation: Callable[[Connection], Any],
    *,
    on_retry: Callable[[], None] | None = None,
) -> Any:
    attempts = _read_int_env(
        "CI_DASHBOARD_POD_WATCH_DB_RETRY_ATTEMPTS",
        DEFAULT_DB_RETRY_ATTEMPTS,
    )
    base_delay_ms = _read_int_env(
        "CI_DASHBOARD_POD_WATCH_DB_RETRY_BASE_DELAY_MS",
        DEFAULT_DB_RETRY_BASE_DELAY_MS,
    )
    max_delay_ms = _read_int_env(
        "CI_DASHBOARD_POD_WATCH_DB_RETRY_MAX_DELAY_MS",
        DEFAULT_DB_RETRY_MAX_DELAY_MS,
    )
    for attempt in range(1, attempts + 1):
        try:
            with engine.begin() as connection:
                return operation(connection)
        except OperationalError as exc:
            if attempt >= attempts or not _is_retryable_db_error(exc):
                raise
            logger.warning(
                "retrying pod watcher DB write after retryable DB error "
                "(attempt %s/%s): %s",
                attempt + 1,
                attempts,
                getattr(exc, "orig", exc),
            )
            if on_retry is not None:
                on_retry()
            retry_delay_seconds = min(
                (base_delay_ms / 1000.0) * (2 ** (attempt - 1)),
                max_delay_ms / 1000.0,
            )
            time.sleep(retry_delay_seconds)
    raise RuntimeError("pod watcher DB retry loop exited unexpectedly")


def _is_retryable_db_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    code = args[0] if args and isinstance(args[0], int) else None
    return code in RETRYABLE_MYSQL_ERROR_CODES


def _build_lifecycle_rows_for_snapshots(
    connection: Connection,
    snapshots: list[WatchedPodSnapshot],
    *,
    jenkins_pod_name_url_prefixes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    snapshot_by_identity = {snapshot.identity: snapshot.snapshot for snapshot in snapshots}
    identities = _sort_identities(snapshot_by_identity)
    event_rows = _build_lifecycle_rows(
        connection,
        identities,
        pod_metadata_by_identity=snapshot_by_identity,
        jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
    )
    event_rows_by_identity = {
        _pod_identity_from_values(
            source_project=_coerce_str(row.get("source_project")) or "",
            namespace_name=_coerce_str(row.get("namespace_name")),
            pod_uid=_coerce_str(row.get("pod_uid")),
            pod_name=_coerce_str(row.get("pod_name")),
        ): row
        for row in event_rows
    }

    metadata_only_rows = [
        _base_lifecycle_row_for_snapshot(snapshot)
        for snapshot in snapshots
        if snapshot.identity not in event_rows_by_identity
    ]
    build_metadata_by_identity = _load_build_metadata_map(
        connection,
        metadata_only_rows,
        pod_metadata_by_identity=snapshot_by_identity,
        jenkins_pod_name_url_prefixes=jenkins_pod_name_url_prefixes,
    )

    rows = list(event_rows)
    for row in metadata_only_rows:
        identity = _pod_identity_from_values(
            source_project=_coerce_str(row.get("source_project")) or "",
            namespace_name=_coerce_str(row.get("namespace_name")),
            pod_uid=_coerce_str(row.get("pod_uid")),
            pod_name=_coerce_str(row.get("pod_name")),
        )
        build_meta = build_metadata_by_identity.get(identity, {})
        namespace_name = _coerce_str(row.get("namespace_name"))
        row.update(
            {
                "build_system": build_meta.get("build_system")
                or _infer_pod_build_system(namespace_name),
                "source_prow_job_id": build_meta.get("source_prow_job_id"),
                "normalized_build_url": build_meta.get("normalized_build_url"),
                "repo_full_name": build_meta.get("repo_full_name"),
                "job_name": canonicalize_job_name(
                    _coerce_str(build_meta.get("job_name")),
                    repo_full_name=_coerce_str(build_meta.get("repo_full_name")),
                ),
            }
        )
        _supplement_jenkins_pod_fields_from_build_metadata(
            row,
            build_meta,
            build_system=_coerce_str(row.get("build_system")),
        )
        rows.append(row)
    return rows


def _base_lifecycle_row_for_snapshot(snapshot: WatchedPodSnapshot) -> dict[str, Any]:
    return {
        "source_project": snapshot.source_project,
        "cluster_name": snapshot.cluster_name,
        "location": snapshot.location,
        "namespace_name": snapshot.namespace_name,
        "pod_name": snapshot.pod_name,
        "pod_uid": snapshot.pod_uid,
        "build_system": _infer_pod_build_system(snapshot.namespace_name),
        **snapshot.snapshot.as_lifecycle_fields(),
        "source_prow_job_id": None,
        "normalized_build_url": None,
        "repo_full_name": None,
        "job_name": None,
        "scheduled_at": None,
        "first_pulling_at": None,
        "first_pulled_at": None,
        "first_created_at": None,
        "first_started_at": None,
        "last_failed_scheduling_at": None,
        "failed_scheduling_count": 0,
        "last_event_at": None,
        "schedule_to_started_seconds": None,
    }


def _load_existing_pod_metadata_snapshots(
    connection: Connection,
    identities: list[tuple[str, str | None, str | None, str | None]],
) -> dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot]:
    if not identities:
        return {}
    requested_pods_sql, params = _build_requested_pods_relation(identities)
    rows = connection.execute(
        text(
            f"""
            WITH requested_pods AS (
              {requested_pods_sql}
            )
            SELECT
              lifecycle.source_project,
              lifecycle.namespace_name,
              lifecycle.pod_uid,
              lifecycle.pod_name,
              lifecycle.pod_labels_json,
              lifecycle.pod_annotations_json,
              lifecycle.metadata_observed_at,
              lifecycle.pod_created_at
            FROM ci_l1_pod_lifecycle AS lifecycle
            JOIN requested_pods AS requested
              ON lifecycle.source_project = requested.source_project
             AND {_null_safe_equals_sql('lifecycle.namespace_name', 'requested.namespace_name', connection.dialect.name)}
             AND {_null_safe_equals_sql('lifecycle.pod_uid', 'requested.pod_uid', connection.dialect.name)}
             AND {_null_safe_equals_sql('lifecycle.pod_name', 'requested.pod_name', connection.dialect.name)}
            """
        ),
        params,
    ).mappings()

    snapshots: dict[tuple[str, str | None, str | None, str | None], PodMetadataSnapshot] = {}
    for row in rows:
        labels = _json_loads_str_mapping(row.get("pod_labels_json"))
        annotations = _json_loads_str_mapping(row.get("pod_annotations_json"))
        creation_timestamp = _parse_datetime(row.get("pod_created_at"))
        if not labels and not annotations and creation_timestamp is None:
            continue
        identity = _pod_identity_from_values(
            source_project=_coerce_str(row.get("source_project")) or "",
            namespace_name=_coerce_str(row.get("namespace_name")),
            pod_uid=_coerce_str(row.get("pod_uid")),
            pod_name=_coerce_str(row.get("pod_name")),
        )
        snapshots[identity] = PodMetadataSnapshot(
            pod_uid=_coerce_str(row.get("pod_uid")),
            labels=labels,
            annotations=annotations,
            observed_at=_parse_datetime(row.get("metadata_observed_at"))
            or datetime.now(UTC).replace(tzinfo=None),
            creation_timestamp=creation_timestamp,
        )
    return snapshots


def _normalize_pod_object(
    pod_object: dict[str, Any],
    *,
    context: WatchRuntimeContext,
) -> WatchedPodSnapshot | None:
    metadata = pod_object.get("metadata")
    if not isinstance(metadata, dict):
        return None
    namespace_name = _coerce_str(metadata.get("namespace"))
    pod_name = _coerce_str(metadata.get("name"))
    pod_uid = _coerce_str(metadata.get("uid"))
    if namespace_name is None or pod_name is None or pod_uid is None:
        return None

    observed_at = datetime.now(UTC).replace(tzinfo=None)
    return WatchedPodSnapshot(
        source_project=context.source_project,
        cluster_name=context.cluster_name,
        location=context.location,
        namespace_name=namespace_name,
        pod_name=pod_name,
        pod_uid=pod_uid,
        snapshot=PodMetadataSnapshot(
            pod_uid=pod_uid,
            labels=_coerce_str_mapping(metadata.get("labels")),
            annotations=_coerce_str_mapping(metadata.get("annotations")),
            observed_at=observed_at,
            creation_timestamp=_parse_datetime(metadata.get("creationTimestamp")),
        ),
    )


def _normalize_kubernetes_event(
    event_object: dict[str, Any],
    *,
    context: WatchRuntimeContext,
) -> NormalizedPodEvent | None:
    metadata = event_object.get("metadata")
    if not isinstance(metadata, dict):
        return None
    involved_object = event_object.get("involvedObject")
    if not isinstance(involved_object, dict):
        return None
    if _coerce_str(involved_object.get("kind")) != "Pod":
        return None

    event_reason = _coerce_str(event_object.get("reason"))
    if event_reason not in POD_EVENT_REASONS:
        return None

    event_timestamp = (
        _parse_datetime(event_object.get("eventTime"))
        or _parse_datetime(event_object.get("lastTimestamp"))
        or _parse_datetime(event_object.get("firstTimestamp"))
        or _parse_datetime(metadata.get("creationTimestamp"))
    )
    if event_timestamp is None:
        return None

    namespace_name = _coerce_str(involved_object.get("namespace")) or _coerce_str(
        metadata.get("namespace")
    )
    pod_name = _coerce_str(involved_object.get("name"))
    pod_uid = _coerce_str(involved_object.get("uid"))
    if namespace_name is None or pod_name is None:
        return None

    source_insert_id = _build_event_source_insert_id(event_object)
    if source_insert_id is None:
        return None

    source = event_object.get("source")
    source_component = None
    source_host = None
    if isinstance(source, dict):
        source_component = _coerce_str(source.get("component"))
        source_host = _coerce_str(source.get("host"))

    return NormalizedPodEvent(
        source_project=context.source_project,
        cluster_name=context.cluster_name,
        location=context.location,
        namespace_name=namespace_name,
        pod_name=pod_name,
        pod_uid=pod_uid,
        event_reason=event_reason,
        event_type=_coerce_str(event_object.get("type")),
        event_message=_coerce_str(event_object.get("message")),
        event_timestamp=event_timestamp,
        receive_timestamp=datetime.now(UTC).replace(tzinfo=None),
        first_timestamp=_parse_datetime(event_object.get("firstTimestamp")),
        last_timestamp=_parse_datetime(event_object.get("lastTimestamp")),
        reporting_component=_coerce_str(event_object.get("reportingComponent"))
        or source_component,
        reporting_instance=_coerce_str(event_object.get("reportingInstance")) or source_host,
        source_insert_id=source_insert_id,
    )


def _build_event_source_insert_id(event_object: dict[str, Any]) -> str | None:
    metadata = event_object.get("metadata")
    if not isinstance(metadata, dict):
        return None
    uid = _coerce_str(metadata.get("uid"))
    resource_version = _coerce_str(metadata.get("resourceVersion"))
    if uid is not None and resource_version is not None:
        return f"kubernetes-event:{uid}:{resource_version}"
    if uid is not None:
        return f"kubernetes-event:{uid}"

    namespace_name = _coerce_str(metadata.get("namespace"))
    name = _coerce_str(metadata.get("name"))
    if namespace_name and name and resource_version:
        return f"kubernetes-event:{namespace_name}:{name}:{resource_version}"
    return None


def _stream_watch_json(
    url: str,
    *,
    token: str,
    ca_file: str | None,
    timeout_seconds: int,
) -> Iterable[dict[str, Any]]:
    request = urllib_request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": WATCH_USER_AGENT,
        },
    )
    context = ssl.create_default_context(cafile=ca_file) if ca_file else None
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds, context=context) as response:
            while True:
                line = response.readline()
                if line == b"":
                    break
                if not line.strip():
                    continue
                yield _decode_json_object(line, error_context="Kubernetes watch")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Kubernetes watch failed: HTTP {exc.code}: {body}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Kubernetes watch failed: {exc.reason}") from exc


def _start_health_server(health_state: WatchHealthState) -> ThreadingHTTPServer | None:
    port = _read_non_negative_int_env("CI_DASHBOARD_POD_WATCH_HEALTH_PORT", DEFAULT_HEALTH_PORT)
    if port == 0:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", port), _build_health_handler(health_state))
    thread = threading.Thread(
        target=server.serve_forever,
        name="pod-watch:health",
        daemon=True,
    )
    thread.start()
    return server


def _build_health_handler(health_state: WatchHealthState):
    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in {"/livez", "/readyz", "/healthz"}:
                self._send_health(health_state.snapshot())
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_health(self, payload: dict[str, Any]) -> None:
            status = HTTPStatus.OK if payload["healthy"] else HTTPStatus.SERVICE_UNAVAILABLE
            encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return _HealthHandler


def _load_runtime_context() -> WatchRuntimeContext:
    source_project = _coerce_str(os.environ.get("CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT")) or _coerce_str(
        os.environ.get("CI_DASHBOARD_GCP_PROJECT")
    )
    if source_project is None:
        raise RuntimeError(
            "Missing CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT or CI_DASHBOARD_GCP_PROJECT"
        )
    return WatchRuntimeContext(
        source_project=source_project,
        cluster_name=_coerce_str(os.environ.get("CI_DASHBOARD_KUBERNETES_CLUSTER_NAME")),
        location=_coerce_str(os.environ.get("CI_DASHBOARD_KUBERNETES_LOCATION")),
    )


def _format_watch_error(watch_event: dict[str, Any]) -> str:
    raw_object = watch_event.get("object")
    if isinstance(raw_object, dict):
        message = _coerce_str(raw_object.get("message"))
        reason = _coerce_str(raw_object.get("reason"))
        code = _coerce_str(raw_object.get("code"))
        return json.dumps(
            {"reason": reason, "message": message, "code": code},
            sort_keys=True,
        )
    return "unknown Kubernetes watch error"


def _is_resource_version_expired_watch_error(watch_event: dict[str, Any]) -> bool:
    raw_object = watch_event.get("object")
    if not isinstance(raw_object, dict):
        return False
    code = _coerce_str(raw_object.get("code"))
    reason = _coerce_str(raw_object.get("reason"))
    return code == "410" or reason in {"Expired", "Gone"}


def _sleep_until_retry(stop_event: threading.Event) -> None:
    retry_delay = _read_int_env(
        "CI_DASHBOARD_POD_WATCH_RETRY_DELAY_SECONDS",
        DEFAULT_WATCH_RETRY_DELAY_SECONDS,
    )
    stop_event.wait(retry_delay)


def _read_non_negative_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _stream_key(namespace_name: str, resource: str) -> str:
    return f"{namespace_name}/{resource}"


def _sort_identities(
    identities: Iterable[tuple[str, str | None, str | None, str | None]],
) -> list[tuple[str, str | None, str | None, str | None]]:
    return sorted(identities, key=lambda item: tuple(value or "" for value in item))


def _get_jenkins_prefixes_if_needed(
    connection: Connection,
    identities: Iterable[tuple[str, str | None, str | None, str | None]],
    jenkins_prefix_cache: _JenkinsPodNameUrlPrefixCache | None,
) -> dict[str, str] | None:
    if jenkins_prefix_cache is None:
        return None
    if any(_infer_pod_build_system(namespace_name) == "JENKINS" for _, namespace_name, _, _ in identities):
        return jenkins_prefix_cache.get(connection)
    return None
