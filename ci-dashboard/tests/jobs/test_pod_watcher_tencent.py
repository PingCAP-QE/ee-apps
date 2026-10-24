from __future__ import annotations

import io
import threading
from datetime import datetime, timedelta
from urllib import error as urllib_error
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs import pod_watcher as watcher
from ci_dashboard.jobs.pod_source import (
    cloud_phase_for_source,
    jenkins_host_for_cloud,
    validate_watch_source,
)
from ci_dashboard.jobs.state_store import get_job_state

SOURCE = "tencent:ap-beijing:cls-9zpon9y1"


def _context() -> watcher.WatchRuntimeContext:
    return watcher.WatchRuntimeContext(SOURCE, "pingcap-ee", "ap-beijing", "TENCENT")


def _unauthorized(code: int = 401) -> RuntimeError:
    cause = urllib_error.HTTPError("https://kubernetes.test", code, "denied", {}, io.BytesIO(b"denied"))
    error = RuntimeError(f"Kubernetes request failed: HTTP {code}")
    error.__cause__ = cause
    return error


def _client(**kwargs) -> watcher._KubernetesClient:
    return watcher._KubernetesClient(
        base_url="https://kubernetes.test", ca_file=None, watch_timeout_seconds=30, **kwargs,
    )


def _event(reason: str = "Scheduled") -> dict:
    return {
        "metadata": {
            "uid": "event-a", "resourceVersion": "2", "namespace": "jenkins-tiflow",
            "creationTimestamp": "2026-10-23T12:00:00Z",
        },
        "involvedObject": {
            "kind": "Pod", "namespace": "jenkins-tiflow", "name": "agent-a", "uid": "pod-a",
        },
        "reason": reason,
        "type": "Normal",
        "eventTime": "2026-10-23T12:00:00Z",
        "series": {"count": 12, "lastObservedTime": "2026-10-23T12:00:30Z"},
    }


def test_source_identity_and_controller_contract():
    assert cloud_phase_for_source("pingcap-testing-account") == "GCP"
    assert cloud_phase_for_source(SOURCE) == "TENCENT"
    assert jenkins_host_for_cloud("GCP") == "https://prow.tidb.net"
    assert jenkins_host_for_cloud("TENCENT") == "https://do.pingcap.net"
    validate_watch_source(SOURCE, cloud_phase="TENCENT", cluster_name="pingcap-ee", location="ap-beijing")
    with pytest.raises(ValueError, match="cloud phase"):
        jenkins_host_for_cloud("OTHER")


@pytest.mark.parametrize("source", [
    "", " ", " leading", "trailing ", "bad\nsource", "a" * 129,
    "tencent:", "tencent:ap-beijing", "tencent:ap-beijing:node-a",
    "tencent:ap-beijing:cls-a:extra", "TENCENT:ap-beijing:cls-a",
    "tencent:ap-beijing:cls-" + "x" * 40,
])
def test_invalid_source_id_is_rejected(source):
    with pytest.raises(ValueError):
        cloud_phase_for_source(source)


@pytest.mark.parametrize("phase,cluster,location", [
    ("GCP", "pingcap-ee", "ap-beijing"),
    ("TENCENT", None, "ap-beijing"),
    ("TENCENT", "pingcap-ee", None),
    ("TENCENT", "pingcap-ee", "ap-shanghai"),
    ("OTHER", "pingcap-ee", "ap-beijing"),
])
def test_watch_source_requires_consistent_configuration(phase, cluster, location):
    with pytest.raises(ValueError):
        validate_watch_source(SOURCE, cloud_phase=phase, cluster_name=cluster, location=location)


def test_runtime_context_uses_explicit_tencent_source_and_scoped_state(monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_CLOUD_PHASE", "TENCENT")
    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT", SOURCE)
    monkeypatch.setenv("CI_DASHBOARD_GCP_PROJECT", "unrelated-gcp-project")
    monkeypatch.setenv("CI_DASHBOARD_KUBERNETES_CLUSTER_NAME", "pingcap-ee")
    monkeypatch.setenv("CI_DASHBOARD_KUBERNETES_LOCATION", "ap-beijing")
    context = watcher._load_runtime_context()
    assert context == _context()
    assert context.job_name == f"ci-watch-pods:{SOURCE}"
    monkeypatch.delenv("CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT")
    with pytest.raises(RuntimeError, match="Missing"):
        watcher._load_runtime_context()


def test_invalid_runtime_context_fails_before_database_access(monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT", SOURCE)
    monkeypatch.delenv("CI_DASHBOARD_POD_WATCH_CLOUD_PHASE", raising=False)

    class NoDatabase:
        def begin(self):
            raise AssertionError("invalid collector must not touch the database")

    with pytest.raises(RuntimeError, match="disagree"):
        watcher.run_watch_pods(NoDatabase(), None)


@pytest.mark.parametrize("fails", [False, True])
def test_tencent_job_updates_do_not_overwrite_existing_gcp_state(sqlite_engine, monkeypatch, fails):
    monkeypatch.setattr(watcher, "_load_runtime_context", _context)
    monkeypatch.setattr(watcher, "_get_kubernetes_api_url", lambda: "https://kubernetes.test")
    monkeypatch.setattr(watcher, "_get_kubernetes_api_ca_file", lambda: None)
    monkeypatch.setattr(watcher, "_load_target_namespaces", lambda connection: ["jenkins-tiflow"])
    monkeypatch.setattr(watcher, "_start_health_server", lambda state: None)
    def pod_worker(**kwargs):
        if fails:
            raise RuntimeError("worker stopped")

    monkeypatch.setattr(watcher, "_run_pod_worker", pod_worker)
    monkeypatch.setattr(watcher, "_run_event_worker", lambda **kwargs: None)
    with sqlite_engine.begin() as connection:
        connection.execute(text("INSERT INTO ci_job_state(job_name, watermark_json, last_status) VALUES ('ci-watch-pods', '{}', 'failed')"))
    settings = Settings(
        database=DatabaseSettings(url="sqlite://", host=None, port=None, user=None, password=None, database=None, ssl_ca=None),
        jobs=JobSettings(), log_level="INFO",
    )
    if fails:
        with pytest.raises(RuntimeError, match="worker stopped"):
            watcher.run_watch_pods(sqlite_engine, settings)
    else:
        watcher.run_watch_pods(sqlite_engine, settings)
    with sqlite_engine.begin() as connection:
        assert get_job_state(connection, "ci-watch-pods").last_status == "failed"
        assert get_job_state(connection, _context().job_name).last_status == ("failed" if fails else "succeeded")


def test_saved_metadata_without_observation_time_is_not_fabricated(sqlite_engine):
    identity = ("gcp-project", "prow-test-pods", "uid-a", "agent-a")
    with sqlite_engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO ci_l1_pod_lifecycle
              (source_project, namespace_name, pod_uid, pod_name, pod_labels_json)
            VALUES ('gcp-project', 'prow-test-pods', 'uid-a', 'agent-a', '{"author":"legacy"}')
        """))
        saved = watcher._load_existing_pod_metadata_snapshots(connection, [identity])
    assert saved[identity].labels == {"author": "legacy"}
    assert saved[identity].observed_at is None


def test_list_refreshes_token_for_retry_and_each_continuation_page(monkeypatch):
    tokens = iter(["expired", "fresh", "rotated"])
    calls = []

    def get_json(url, *, headers, ca_file):
        token = headers["Authorization"]
        calls.append((url, token))
        if token == "Bearer expired":
            raise _unauthorized()
        if token == "Bearer fresh":
            return {"items": [{"name": "a"}], "metadata": {"continue": "page-2", "resourceVersion": "10"}}
        return {"items": [{"name": "b"}], "metadata": {"resourceVersion": "10"}}

    monkeypatch.setattr(watcher, "_get_json", get_json)
    items, rv = _client(token_provider=lambda: next(tokens)).list_collection(namespace_name="ci", resource="pods")
    assert items == [{"name": "a"}, {"name": "b"}]
    assert rv == "10"
    assert [token for _, token in calls] == ["Bearer expired", "Bearer fresh", "Bearer rotated"]
    assert "continue=page-2" in calls[-1][0]


def test_atomic_token_file_rotation_is_observed_on_next_request(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("old")
    tokens = []

    def get_json(url, *, headers, ca_file):
        tokens.append(headers["Authorization"])
        return {"items": [], "metadata": {"resourceVersion": "1"}}

    def stream(url, *, token, ca_file, timeout_seconds):
        tokens.append("Bearer " + token)
        yield {"type": "BOOKMARK"}

    monkeypatch.setattr(watcher, "_get_json", get_json)
    monkeypatch.setattr(watcher, "_stream_watch_json", stream)
    client = _client(token_provider=path.read_text)
    client.list_collection(namespace_name="ci", resource="pods")
    replacement = tmp_path / "replacement"
    replacement.write_text("new")
    replacement.replace(path)
    list(client.stream_collection(namespace_name="ci", resource="pods", resource_version="1"))
    assert tokens == ["Bearer old", "Bearer new"]


@pytest.mark.parametrize("code,attempts", [(401, 2), (403, 1)])
def test_persistent_list_auth_error_has_bounded_retry(monkeypatch, code, attempts):
    calls = []

    def get_json(*args, **kwargs):
        calls.append(kwargs)
        raise _unauthorized(code)

    monkeypatch.setattr(watcher, "_get_json", get_json)
    with pytest.raises(RuntimeError, match=f"HTTP {code}"):
        _client(token_provider=lambda: "token").list_collection(namespace_name="ci", resource="pods")
    assert len(calls) == attempts


@pytest.mark.parametrize("value", ["", " \n "])
def test_empty_token_is_never_sent(monkeypatch, value):
    monkeypatch.setattr(watcher, "_get_json", lambda *args, **kwargs: pytest.fail("empty bearer was sent"))
    with pytest.raises(RuntimeError, match="token is empty"):
        _client(token_provider=lambda: value).list_collection(namespace_name="ci", resource="pods")


def test_missing_token_file_fails_without_request(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "_get_json", lambda *args, **kwargs: pytest.fail("request must not be sent"))
    with pytest.raises(OSError):
        _client(token_provider=(tmp_path / "missing").read_text).list_collection(namespace_name="ci", resource="pods")


def test_watch_status_401_refreshes_once_from_processed_resource_version(monkeypatch):
    calls = []
    closed = []
    tokens = iter(["old", "new"])

    def stream(url, *, token, ca_file, timeout_seconds):
        calls.append((url, token))
        if token == "old":
            try:
                yield {"type": "ADDED", "object": {"metadata": {"resourceVersion": "2"}}}
                yield {"type": "ERROR", "object": {"code": 401}}
            finally:
                closed.append(token)
        else:
            yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "3"}}}

    monkeypatch.setattr(watcher, "_stream_watch_json", stream)
    result = list(_client(token_provider=lambda: next(tokens)).stream_collection(namespace_name="ci", resource="pods", resource_version="1"))
    assert [event["type"] for event in result] == ["ADDED", "BOOKMARK"]
    assert [token for _, token in calls] == ["old", "new"]
    assert parse_qs(urlparse(calls[-1][0]).query)["resourceVersion"] == ["2"]
    assert closed == ["old"]


@pytest.mark.parametrize("code,attempts", [(401, 2), (403, 1)])
def test_watch_http_auth_errors_have_bounded_retry(monkeypatch, code, attempts):
    calls = []

    def stream(*args, **kwargs):
        calls.append(kwargs)
        raise _unauthorized(code)
        yield  # pragma: no cover

    monkeypatch.setattr(watcher, "_stream_watch_json", stream)
    with pytest.raises(RuntimeError, match=f"HTTP {code}"):
        list(_client(token_provider=lambda: "token").stream_collection(namespace_name="ci", resource="pods", resource_version="1"))
    assert len(calls) == attempts


def test_second_watch_status_401_is_returned_to_worker_backoff(monkeypatch):
    calls = []

    def stream(*args, **kwargs):
        calls.append(kwargs)
        yield {"type": "ERROR", "object": {"code": 401}}

    monkeypatch.setattr(watcher, "_stream_watch_json", stream)
    result = list(_client(token_provider=lambda: "token").stream_collection(namespace_name="ci", resource="events", resource_version="1"))
    assert result == [{"type": "ERROR", "object": {"code": 401}}]
    assert len(calls) == 2


@pytest.mark.parametrize("reason", sorted(watcher.TENCENT_POD_EVENT_REASONS))
def test_tencent_diagnostics_do_not_expand_gcp_allowlist(reason):
    event = _event(reason)
    assert watcher._normalize_kubernetes_event(event, context=_context()).event_reason == reason
    assert watcher._normalize_kubernetes_event(event, context=watcher.WatchRuntimeContext("gcp-project", "prow", "us-central1")) is None


def test_event_series_keeps_first_time_and_latest_recurrence():
    row = watcher._normalize_kubernetes_event(_event(), context=_context())
    assert row.first_timestamp == datetime(2026, 10, 23, 12)
    assert row.last_timestamp == datetime(2026, 10, 23, 12, 0, 30)
    assert row.event_timestamp == datetime(2026, 10, 23, 12)
    event = _event()
    event.pop("eventTime")
    event.pop("series")
    event["firstTimestamp"] = "2026-10-23T12:00:05Z"
    event["lastTimestamp"] = "2026-10-23T12:00:20Z"
    row = watcher._normalize_kubernetes_event(event, context=_context())
    assert row.event_timestamp == datetime(2026, 10, 23, 12, 0, 20)
    assert row.first_timestamp == datetime(2026, 10, 23, 12, 0, 5)
    assert row.last_timestamp == datetime(2026, 10, 23, 12, 0, 20)


@pytest.mark.parametrize("worker", [watcher._run_pod_worker, watcher._run_event_worker])
@pytest.mark.parametrize("failure_phase", ["list", "watch"])
def test_http_410_immediately_relists_without_retry_delay(worker, failure_phase, monkeypatch):
    stop = threading.Event()
    health = watcher.WatchHealthState(stale_after_seconds=60)
    key = "ci/stream"
    health.register(key)
    restarts = []

    def expired():
        error = _unauthorized(410)
        raise error

    class Client:
        def list_collection(self, **kwargs):
            if failure_phase == "list":
                expired()
            return [], "1"

        def stream_collection(self, **kwargs):
            expired()
            yield  # pragma: no cover

    class Recorder:
        def add(self, **kwargs):
            if kwargs.get("watch_restarts"):
                restarts.append(kwargs)
                stop.set()

    monkeypatch.setattr(watcher, "_sleep_until_retry", lambda stop: pytest.fail("410 must relist immediately"))
    worker(
        engine=None, settings=None, client=Client(), context=_context(), namespace_name="ci",
        stream_key=key, health_state=health, recorder=Recorder(),
        jenkins_prefix_cache=None, stop_event=stop,
    )
    assert len(restarts) == 1


def test_expired_pagination_discards_partial_snapshot_before_fresh_list(monkeypatch):
    stop = threading.Event()
    health = watcher.WatchHealthState(stale_after_seconds=60)
    health.register("ci/pods")
    calls = []
    persisted = []
    restarts = []

    def pod(name):
        return {"metadata": {"name": name, "namespace": "ci", "uid": name}}

    def get_json(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return {"items": [pod("discard-me")], "metadata": {"continue": "expired", "resourceVersion": "1"}}
        if len(calls) == 2:
            raise _unauthorized(410)
        return {"items": [pod("fresh")], "metadata": {"resourceVersion": "2"}}

    def stream(*args, **kwargs):
        yield {"type": "BOOKMARK"}
        stop.set()

    def persist(engine, settings, snapshots, **kwargs):
        persisted.append([snapshot.pod_name for snapshot in snapshots])
        return len(snapshots)

    class Recorder:
        def add(self, **kwargs):
            if kwargs.get("watch_restarts"):
                restarts.append(kwargs)

    monkeypatch.setattr(watcher, "_get_json", get_json)
    monkeypatch.setattr(watcher, "_stream_watch_json", stream)
    monkeypatch.setattr(watcher, "_persist_pod_snapshots", persist)
    monkeypatch.setattr(watcher, "_sleep_until_retry", lambda stop: pytest.fail("pagination expiry must relist"))
    watcher._run_pod_worker(
        engine=None, settings=None, client=_client(token="token"), context=_context(),
        namespace_name="ci", stream_key="ci/pods", health_state=health, recorder=Recorder(),
        jenkins_prefix_cache=None, stop_event=stop,
    )
    assert persisted == [["fresh"]]
    assert len(restarts) == 1
    assert "continue=expired" in calls[1]
    assert "continue=" not in calls[2]


@pytest.mark.parametrize("worker", [watcher._run_pod_worker, watcher._run_event_worker])
def test_empty_namespace_heartbeat_does_not_require_pod_activity(worker):
    stop = threading.Event()
    health = watcher.WatchHealthState(stale_after_seconds=60)
    key = "ci/stream"
    health.register(key)

    class Client:
        def list_collection(self, **kwargs):
            return [], "1"

        def stream_collection(self, **kwargs):
            yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "1"}}}
            stop.set()

    class Recorder:
        def add(self, **kwargs):
            pass

    worker(
        engine=None, settings=None, client=Client(), context=_context(), namespace_name="ci",
        stream_key=key, health_state=health, recorder=Recorder(),
        jenkins_prefix_cache=None, stop_event=stop,
    )
    assert health.snapshot()["healthy"] is True


def test_prefix_cache_is_partitioned_by_controller_and_reference_window(sqlite_engine, monkeypatch):
    calls = []

    def prefixes(connection, *, cloud_phase, reference_time):
        calls.append((cloud_phase, reference_time))
        return {"agent": jenkins_host_for_cloud(cloud_phase) + "/jenkins/job/ci/"}

    monkeypatch.setattr(watcher, "_load_jenkins_pod_name_url_prefix_map", prefixes)
    cache = watcher._JenkinsPodNameUrlPrefixCache(ttl_seconds=900)
    reference = datetime(2026, 10, 23, 12)
    with sqlite_engine.begin() as connection:
        gcp = cache.get(connection, cloud_phase="GCP", reference_time=reference)
        assert cache.get(connection, cloud_phase="GCP", reference_time=reference + timedelta(seconds=1)) is gcp
        tencent = cache.get(connection, cloud_phase="TENCENT", reference_time=reference)
        assert gcp != tencent
        cache.get(connection, cloud_phase="TENCENT", reference_time=reference + timedelta(days=1))
    assert len(calls) == 3
    assert calls[0] == ("GCP", datetime(2026, 10, 23))
    assert calls[1] == ("TENCENT", datetime(2026, 10, 23))
    assert calls[2] == ("TENCENT", datetime(2026, 10, 24))
