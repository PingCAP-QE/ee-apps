from __future__ import annotations

import json
import socket
import threading
from datetime import datetime
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.common.models import WatchPodsSummary
from ci_dashboard.jobs import pod_watcher as watcher
from ci_dashboard.jobs.pod_watcher import (
    _JenkinsPodNameUrlPrefixCache,
    WatchedPodSnapshot,
    WatchHealthState,
    WatchRuntimeContext,
    _KubernetesClient,
    _ProgressRecorder,
    _build_event_source_insert_id,
    _build_lifecycle_rows_for_snapshots,
    _db_batch_size,
    _format_watch_error,
    _is_retryable_db_error,
    _is_resource_version_expired_watch_error,
    _load_runtime_context,
    _normalize_kubernetes_event,
    _normalize_pod_object,
    _persist_pod_events,
    _persist_pod_snapshots,
    _read_non_negative_int_env,
    _sleep_until_retry,
    _start_health_server,
    _stream_key,
    _stream_watch_json,
)
from ci_dashboard.jobs.sync_pods import NormalizedPodEvent, PodMetadataSnapshot


def _settings(batch_size: int = 100) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(batch_size=batch_size),
        log_level="INFO",
    )


def _runtime_context() -> WatchRuntimeContext:
    return WatchRuntimeContext(
        source_project="pingcap-testing-account",
        cluster_name="prow",
        location="us-central1-c",
    )


def _watched_snapshot(
    *,
    namespace_name: str = "jenkins-tidb",
    pod_name: str = "agent-pod",
    pod_uid: str = "uid-a",
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    observed_at: datetime = datetime(2026, 5, 1, 1, 0, 2),
    creation_timestamp: datetime = datetime(2026, 5, 1, 1, 0, 1),
) -> WatchedPodSnapshot:
    return WatchedPodSnapshot(
        source_project="pingcap-testing-account",
        cluster_name="prow",
        location="us-central1-c",
        namespace_name=namespace_name,
        pod_name=pod_name,
        pod_uid=pod_uid,
        snapshot=PodMetadataSnapshot(
            pod_uid=pod_uid,
            labels=labels or {},
            annotations=annotations or {},
            observed_at=observed_at,
            creation_timestamp=creation_timestamp,
        ),
    )


def _pod_event(
    *,
    namespace_name: str = "jenkins-tidb",
    pod_name: str = "agent-pod",
    pod_uid: str = "uid-a",
    reason: str = "Scheduled",
    timestamp: datetime = datetime(2026, 5, 1, 1, 0, 5),
    insert_id: str = "kubernetes-event:sched",
    component: str = "default-scheduler",
    instance: str = "scheduler",
    message: str = "Successfully assigned",
) -> NormalizedPodEvent:
    return NormalizedPodEvent(
        source_project="pingcap-testing-account",
        cluster_name="prow",
        location="us-central1-c",
        namespace_name=namespace_name,
        pod_name=pod_name,
        pod_uid=pod_uid,
        event_reason=reason,
        event_type="Normal",
        event_message=message,
        event_timestamp=timestamp,
        receive_timestamp=timestamp,
        first_timestamp=timestamp,
        last_timestamp=timestamp,
        reporting_component=component,
        reporting_instance=instance,
        source_insert_id=insert_id,
    )


def test_watch_health_state_requires_each_stream_heartbeat() -> None:
    health_state = WatchHealthState(stale_after_seconds=60)
    health_state.register("jenkins-tidb/pods")
    health_state.register("jenkins-tidb/events")

    assert health_state.snapshot()["healthy"] is False

    health_state.heartbeat("jenkins-tidb/pods")
    assert health_state.snapshot()["healthy"] is False

    health_state.heartbeat("jenkins-tidb/events")
    snapshot = health_state.snapshot()
    assert snapshot["healthy"] is True
    assert snapshot["streams"]["jenkins-tidb/pods"]["healthy"] is True
    assert snapshot["streams"]["jenkins-tidb/events"]["healthy"] is True


def test_progress_recorder_sets_stop_event_at_max_events() -> None:
    summary = WatchPodsSummary()
    stop_event = threading.Event()
    recorder = _ProgressRecorder(summary, max_events=3, stop_event=stop_event)

    recorder.add(pod_snapshots_seen=1, event_rows_seen=1, pods_touched=2)
    assert stop_event.is_set() is False

    recorder.add(event_rows_seen=1, event_rows_written=1, lifecycle_rows_upserted=1, watch_restarts=1)
    assert stop_event.is_set() is True
    assert summary.pod_snapshots_seen == 1
    assert summary.event_rows_seen == 2
    assert summary.event_rows_written == 1
    assert summary.lifecycle_rows_upserted == 1
    assert summary.pods_touched == 2
    assert summary.watch_restarts == 1


def test_db_batch_size_defaults_to_watcher_specific_cap(monkeypatch) -> None:
    monkeypatch.delenv("CI_DASHBOARD_POD_WATCH_DB_BATCH_SIZE", raising=False)
    assert _db_batch_size(_settings(batch_size=1000)) == 100

    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_DB_BATCH_SIZE", "25")
    assert _db_batch_size(_settings(batch_size=1000)) == 25


def test_retryable_db_error_detects_tidb_lock_timeout() -> None:
    class _Orig:
        args = (1205, "Lock wait timeout exceeded; try restarting transaction")

    assert _is_retryable_db_error(OperationalError("statement", {}, _Orig())) is True


def test_kubernetes_client_lists_and_streams_collection(monkeypatch) -> None:
    get_urls: list[str] = []
    pages = iter(
        [
            {
                "items": [{"metadata": {"name": "pod-a"}}, "ignore-me"],
                "metadata": {"continue": "next", "resourceVersion": "rv-1"},
            },
            {
                "items": [{"metadata": {"name": "pod-b"}}],
                "metadata": {"resourceVersion": "rv-2"},
            },
        ]
    )

    def fake_get_json(url, *, headers, ca_file):
        get_urls.append(url)
        assert headers == {"Authorization": "Bearer token"}
        assert ca_file == "/tmp/ca.crt"
        return next(pages)

    stream_calls: list[dict[str, object]] = []

    def fake_stream_watch_json(url, *, token, ca_file, timeout_seconds):
        stream_calls.append(
            {
                "url": url,
                "token": token,
                "ca_file": ca_file,
                "timeout_seconds": timeout_seconds,
            }
        )
        yield {"type": "BOOKMARK"}

    monkeypatch.setattr(watcher, "_get_json", fake_get_json)
    monkeypatch.setattr(watcher, "_stream_watch_json", fake_stream_watch_json)

    client = _KubernetesClient(
        base_url="https://k8s.example/",
        token="token",
        ca_file="/tmp/ca.crt",
        watch_timeout_seconds=60,
    )
    items, resource_version = client.list_collection(namespace_name="jenkins tidb", resource="pods")
    assert [item["metadata"]["name"] for item in items] == ["pod-a", "pod-b"]
    assert resource_version == "rv-2"
    assert "continue=next" in get_urls[1]
    assert "jenkins%20tidb" in get_urls[0]

    assert list(client.stream_collection(namespace_name="jenkins-tidb", resource="events", resource_version="rv-2")) == [
        {"type": "BOOKMARK"}
    ]
    assert stream_calls == [
        {
            "url": (
                "https://k8s.example/api/v1/namespaces/jenkins-tidb/events?"
                "watch=true&allowWatchBookmarks=true&timeoutSeconds=60&resourceVersion=rv-2"
            ),
            "token": "token",
            "ca_file": "/tmp/ca.crt",
            "timeout_seconds": 90,
        }
    ]


class _FakeWatchResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __enter__(self) -> "_FakeWatchResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


def test_stream_watch_json_decodes_lines_and_wraps_errors(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_urlopen(request, timeout=0, context=None):
        observed["authorization"] = request.headers["Authorization"]
        observed["timeout"] = timeout
        observed["context"] = context
        return _FakeWatchResponse([b"\n", b'{"type":"ADDED"}\n'])

    monkeypatch.setattr(watcher.urllib_request, "urlopen", fake_urlopen)

    assert list(_stream_watch_json("https://k8s.example/watch", token="token", ca_file=None, timeout_seconds=3)) == [
        {"type": "ADDED"}
    ]
    assert observed["authorization"] == "Bearer token"
    assert observed["timeout"] == 3

    monkeypatch.setattr(
        watcher.urllib_request,
        "urlopen",
        lambda request, timeout=0, context=None: (_ for _ in ()).throw(
            urllib_error.URLError("network down")
        ),
    )
    try:
        list(_stream_watch_json("https://k8s.example/watch", token="token", ca_file=None, timeout_seconds=3))
    except RuntimeError as exc:
        assert "network down" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_health_server_reports_stream_health(monkeypatch) -> None:
    health_state = WatchHealthState(stale_after_seconds=60)
    health_state.register("jenkins-tidb/pods")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_HEALTH_PORT", str(port))
    server = _start_health_server(health_state)
    assert server is not None
    try:
        try:
            urllib_request.urlopen(f"http://127.0.0.1:{port}/livez", timeout=2)
        except urllib_error.HTTPError as exc:
            assert exc.code == 503
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["healthy"] is False
        else:
            raise AssertionError("expected unhealthy response")

        health_state.heartbeat("jenkins-tidb/pods")
        with urllib_request.urlopen(f"http://127.0.0.1:{port}/readyz?verbose=1", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert payload["healthy"] is True

        try:
            urllib_request.urlopen(f"http://127.0.0.1:{port}/missing", timeout=2)
        except urllib_error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("expected not found response")
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_context_and_helper_edge_cases(monkeypatch) -> None:
    monkeypatch.delenv("CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT", raising=False)
    monkeypatch.delenv("CI_DASHBOARD_GCP_PROJECT", raising=False)
    try:
        _load_runtime_context()
    except RuntimeError as exc:
        assert "Missing CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT" in str(exc)
    else:
        raise AssertionError("expected missing project error")

    monkeypatch.setenv("CI_DASHBOARD_GCP_PROJECT", "from-gcp")
    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT", "from-watch")
    monkeypatch.setenv("CI_DASHBOARD_KUBERNETES_CLUSTER_NAME", "prow")
    monkeypatch.setenv("CI_DASHBOARD_KUBERNETES_LOCATION", "us-central1-c")
    context = _load_runtime_context()
    assert context.source_project == "from-watch"
    assert context.cluster_name == "prow"
    assert context.location == "us-central1-c"

    monkeypatch.delenv("TEST_NON_NEGATIVE_INT", raising=False)
    assert _read_non_negative_int_env("TEST_NON_NEGATIVE_INT", 7) == 7
    monkeypatch.setenv("TEST_NON_NEGATIVE_INT", "bad")
    assert _read_non_negative_int_env("TEST_NON_NEGATIVE_INT", 7) == 7
    monkeypatch.setenv("TEST_NON_NEGATIVE_INT", "-1")
    assert _read_non_negative_int_env("TEST_NON_NEGATIVE_INT", 7) == 7
    monkeypatch.setenv("TEST_NON_NEGATIVE_INT", "0")
    assert _read_non_negative_int_env("TEST_NON_NEGATIVE_INT", 7) == 0

    assert _stream_key("jenkins-tidb", "pods") == "jenkins-tidb/pods"
    assert _format_watch_error({"object": {"reason": "Gone", "message": "too old", "code": 410}}) == (
        '{"code": "410", "message": "too old", "reason": "Gone"}'
    )
    assert _format_watch_error({"type": "ERROR"}) == "unknown Kubernetes watch error"
    assert _is_resource_version_expired_watch_error({"object": {"reason": "Gone", "code": 410}}) is True
    assert _is_resource_version_expired_watch_error({"object": {"reason": "Forbidden", "code": 403}}) is False
    assert _build_event_source_insert_id({"metadata": {"uid": "uid-a"}}) == "kubernetes-event:uid-a"
    assert _build_event_source_insert_id(
        {"metadata": {"namespace": "ns", "name": "event-name", "resourceVersion": "rv"}}
    ) == "kubernetes-event:ns:event-name:rv"
    assert _build_event_source_insert_id({"metadata": {}}) is None

    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_RETRY_DELAY_SECONDS", "1")
    stop_event = threading.Event()
    stop_event.set()
    _sleep_until_retry(stop_event)


def test_normalize_pod_object_preserves_creation_time_and_metadata() -> None:
    snapshot = _normalize_pod_object(
        {
            "metadata": {
                "namespace": "jenkins-tidb",
                "name": "agent-pod",
                "uid": "uid-a",
                "creationTimestamp": "2026-05-01T01:02:03Z",
                "labels": {"org": "pingcap", "repo": "tidb"},
                "annotations": {"ci_job": "pingcap/tidb/ghpr_unit_test"},
            }
        },
        context=_runtime_context(),
    )

    assert snapshot is not None
    assert snapshot.identity == (
        "pingcap-testing-account",
        "jenkins-tidb",
        "uid-a",
        "agent-pod",
    )
    assert snapshot.snapshot.creation_timestamp == datetime(2026, 5, 1, 1, 2, 3)
    assert snapshot.snapshot.labels == {"org": "pingcap", "repo": "tidb"}
    assert snapshot.snapshot.annotations == {"ci_job": "pingcap/tidb/ghpr_unit_test"}


def test_normalize_kubernetes_event_uses_core_event_fields() -> None:
    normalized = _normalize_kubernetes_event(
        {
            "metadata": {
                "namespace": "jenkins-tidb",
                "name": "agent-pod.17f",
                "uid": "event-uid",
                "resourceVersion": "12345",
                "creationTimestamp": "2026-05-01T01:02:08Z",
            },
            "involvedObject": {
                "kind": "Pod",
                "namespace": "jenkins-tidb",
                "name": "agent-pod",
                "uid": "uid-a",
            },
            "reason": "ImagePullBackOff",
            "type": "Warning",
            "message": "Back-off pulling image",
            "lastTimestamp": "2026-05-01T01:02:10Z",
            "firstTimestamp": "2026-05-01T01:02:09Z",
            "source": {"component": "kubelet", "host": "node-a"},
        },
        context=_runtime_context(),
    )

    assert normalized is not None
    assert normalized.source_project == "pingcap-testing-account"
    assert normalized.namespace_name == "jenkins-tidb"
    assert normalized.pod_name == "agent-pod"
    assert normalized.pod_uid == "uid-a"
    assert normalized.event_reason == "ImagePullBackOff"
    assert normalized.event_timestamp == datetime(2026, 5, 1, 1, 2, 10)
    assert normalized.source_insert_id == "kubernetes-event:event-uid:12345"
    assert normalized.reporting_component == "kubelet"
    assert normalized.reporting_instance == "node-a"


def test_snapshot_lifecycle_keeps_metadata_when_events_arrive_later(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  10, 'prow-job-jenkins-42', 'apps',
                  'ghpr_unit_test', 'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/42/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/42/',
                  'opaque-prow-pod', '2026-05-01T01:00:00Z', 'GCP', 'JENKINS'
                )
                """
            )
        )

    snapshot = _watched_snapshot(
        labels={
            "author": "alice",
            "org": "pingcap",
            "repo": "tidb",
            "jenkins/label": "pingcap_tidb_ghpr_unit_test_42-abcd",
        },
        annotations={
            "buildUrl": "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/42/",
            "ci_job": "pingcap/tidb/ghpr_unit_test",
        },
    )

    assert _persist_pod_snapshots(sqlite_engine, _settings(), [snapshot]) == 1

    rows = [
        _pod_event(),
        _pod_event(
            reason="Started",
            timestamp=datetime(2026, 5, 1, 1, 0, 25),
            insert_id="kubernetes-event:started",
            component="kubelet",
            instance="node-a",
            message="Started container",
        ),
    ]

    assert _persist_pod_events(sqlite_engine, _settings(), rows) == (2, 1, 1)

    with sqlite_engine.begin() as connection:
        lifecycle = connection.execute(
            text(
                """
                SELECT
                  pod_created_at,
                  pod_author,
                  ci_job,
                  source_prow_job_id,
                  normalized_build_url,
                  scheduled_at,
                  first_started_at,
                  schedule_to_started_seconds
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-a'
                """
            )
        ).mappings().one()

    assert str(lifecycle["pod_created_at"]) == "2026-05-01 01:00:01"
    assert lifecycle["pod_author"] == "alice"
    assert lifecycle["ci_job"] == "pingcap/tidb/ghpr_unit_test"
    assert lifecycle["source_prow_job_id"] == "prow-job-jenkins-42"
    assert lifecycle["normalized_build_url"] == (
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/42/"
    )
    assert str(lifecycle["scheduled_at"]) == "2026-05-01 01:00:05"
    assert str(lifecycle["first_started_at"]) == "2026-05-01 01:00:25"
    assert lifecycle["schedule_to_started_seconds"] == 20


def test_build_lifecycle_rows_for_snapshots_uses_existing_event_aggregates(sqlite_engine) -> None:
    snapshot = _watched_snapshot(
        namespace_name="prow-test-pods",
        pod_name="prow-pod",
        pod_uid="uid-prow",
        labels={"prow.k8s.io/job": "ghpr_unit_test"},
    )
    event = _pod_event(
        namespace_name="prow-test-pods",
        pod_name="prow-pod",
        pod_uid="uid-prow",
        insert_id="kubernetes-event:prow-sched",
    )
    _persist_pod_events(sqlite_engine, _settings(), [event])

    with sqlite_engine.begin() as connection:
        rows = _build_lifecycle_rows_for_snapshots(connection, [snapshot])

    assert len(rows) == 1
    assert rows[0]["scheduled_at"] == datetime(2026, 5, 1, 1, 0, 5)
    assert rows[0]["pod_created_at"] == datetime(2026, 5, 1, 1, 0, 1)


def test_persist_paths_cache_jenkins_prefixes_and_refresh_heartbeat(sqlite_engine, monkeypatch) -> None:
    load_calls = 0

    def fake_load_prefixes(connection):
        nonlocal load_calls
        load_calls += 1
        return {
            "ghpr-unit-test": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/"
        }

    monkeypatch.setattr(watcher, "_load_jenkins_pod_name_url_prefix_map", fake_load_prefixes)
    cache = _JenkinsPodNameUrlPrefixCache(ttl_seconds=900)
    heartbeats = 0

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    snapshots = [
        WatchedPodSnapshot(
            source_project="pingcap-testing-account",
            cluster_name="prow",
            location="us-central1-c",
            namespace_name="jenkins-tidb",
            pod_name=f"ghpr-unit-test-1234{index}-abcd",
            pod_uid=f"uid-cache-{index}",
            snapshot=PodMetadataSnapshot(
                pod_uid=f"uid-cache-{index}",
                labels={},
                annotations={},
                observed_at=datetime(2026, 5, 1, 1, index, 2),
                creation_timestamp=datetime(2026, 5, 1, 1, index, 1),
            ),
        )
        for index in range(2)
    ]

    assert (
        _persist_pod_snapshots(
            sqlite_engine,
            _settings(batch_size=1),
            snapshots,
            jenkins_prefix_cache=cache,
            on_batch_persisted=heartbeat,
        )
        == 2
    )
    assert load_calls == 1
    assert heartbeats == 2

    event_cache = _JenkinsPodNameUrlPrefixCache(ttl_seconds=900)
    events = [
        NormalizedPodEvent(
            source_project="pingcap-testing-account",
            cluster_name="prow",
            location="us-central1-c",
            namespace_name="jenkins-tidb",
            pod_name=f"ghpr-unit-test-2234{index}-abcd",
            pod_uid=f"uid-event-cache-{index}",
            event_reason="Scheduled",
            event_type="Normal",
            event_message="Successfully assigned",
            event_timestamp=datetime(2026, 5, 1, 2, index, 5),
            receive_timestamp=datetime(2026, 5, 1, 2, index, 6),
            first_timestamp=datetime(2026, 5, 1, 2, index, 5),
            last_timestamp=datetime(2026, 5, 1, 2, index, 5),
            reporting_component="default-scheduler",
            reporting_instance="scheduler",
            source_insert_id=f"kubernetes-event:cache-{index}",
        )
        for index in range(2)
    ]

    assert (
        _persist_pod_events(
            sqlite_engine,
            _settings(batch_size=1),
            events,
            jenkins_prefix_cache=event_cache,
            on_batch_persisted=heartbeat,
        )
        == (2, 2, 2)
    )
    assert load_calls == 2
    assert heartbeats == 4


def test_persist_pod_events_retries_retryable_db_error(sqlite_engine, monkeypatch) -> None:
    original_upsert = watcher._upsert_pod_events
    calls = 0
    heartbeats = 0
    sleep_calls: list[float] = []

    class _Orig:
        args = (1205, "Lock wait timeout exceeded; try restarting transaction")

    def flaky_upsert(connection, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("statement", {}, _Orig())
        original_upsert(connection, payload)

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_DB_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_DB_RETRY_BASE_DELAY_MS", "1000")
    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_DB_RETRY_MAX_DELAY_MS", "25")
    monkeypatch.setattr(watcher.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(watcher, "_upsert_pod_events", flaky_upsert)

    assert (
        _persist_pod_events(
            sqlite_engine,
            _settings(),
            [_pod_event()],
            on_batch_persisted=heartbeat,
        )
        == (1, 1, 1)
    )
    assert calls == 2
    assert heartbeats == 2
    assert sleep_calls == [0.025]
