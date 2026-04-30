from __future__ import annotations

import io
from datetime import datetime
from datetime import UTC
from urllib import error as urllib_error

import pytest
from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.state_store import get_job_state
from ci_dashboard.jobs.sync_pods import (
    JenkinsPodNameBuildRef,
    POD_EVENT_REASONS,
    PodMetadataSnapshot,
    _build_ci_job_from_build_metadata,
    _build_requested_pods_relation,
    _build_jenkins_url_from_label_and_ci_job,
    _coerce_str_mapping,
    _compute_start_from,
    _decode_json_object,
    _extract_normalized_build_url_from_metadata,
    _extract_normalized_build_url_from_pod_name,
    _extract_project_from_log_name,
    _extract_build_number_from_jenkins_label,
    _fetch_pod_event_entries,
    _format_rfc3339,
    _get_json,
    _get_kubernetes_api_ca_file,
    _get_kubernetes_api_token,
    _get_kubernetes_api_url,
    _infer_pod_build_system,
    _json_dumps_or_none,
    _list_namespace_pod_metadata,
    _load_jenkins_pod_name_url_prefix_map,
    _load_target_namespaces,
    _max_receive_timestamp,
    _normalize_logging_entry,
    _normalize_jenkins_runtime_url,
    _null_safe_equals_sql,
    _parse_jenkins_pod_name_build_ref,
    _post_json,
    _read_int_env,
    _request_json,
    _resolve_jenkins_build_metadata,
    _upsert_pod_events,
    run_reconcile_pod_linkage_for_time_window,
    run_sync_pods,
)


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


def _pod_identity(namespace_name: str, pod_uid: str, pod_name: str) -> tuple[str, str, str, str]:
    return ("pingcap-testing-account", namespace_name, pod_uid, pod_name)


def _pod_metadata(
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    pod_uid: str | None = None,
    creation_timestamp: datetime | None = None,
) -> PodMetadataSnapshot:
    return PodMetadataSnapshot(
        pod_uid=pod_uid,
        labels=labels or {},
        annotations=annotations or {},
        observed_at=datetime(2026, 4, 21, 9, 45, 0),
        creation_timestamp=creation_timestamp,
    )


class _FakeHTTPResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_post_json_retries_transient_url_errors(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_urlopen(request, timeout=0, context=None):
        del request, timeout, context
        calls["count"] += 1
        if calls["count"] < 3:
            raise urllib_error.URLError("temporary")
        return _FakeHTTPResponse('{"ok": true}')

    monkeypatch.setattr("ci_dashboard.jobs.sync_pods.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("ci_dashboard.jobs.sync_pods.urllib_request.urlopen", fake_urlopen)

    assert _post_json("https://logging.googleapis.com/v2/entries:list", {"foo": "bar"}, headers={}) == {
        "ok": True
    }
    assert calls["count"] == 3


def test_get_json_wraps_invalid_json_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods.urllib_request.urlopen",
        lambda request, timeout=0, context=None: _FakeHTTPResponse("<html>temporarily unavailable</html>"),
    )

    with pytest.raises(RuntimeError, match="Kubernetes API returned invalid JSON"):
        _get_json("https://kubernetes.invalid/api/v1/pods", headers={}, ca_file=None)


def test_extract_build_number_from_jenkins_label_requires_non_numeric_suffix() -> None:
    assert _extract_build_number_from_jenkins_label(
        "idb_pull_integration_realcluster_test_next_gen_1029-kz725"
    ) == "1029"
    assert _extract_build_number_from_jenkins_label(
        "idb_pull_integration_realcluster_test_next_gen_1029-12345"
    ) is None
    assert _extract_build_number_from_jenkins_label(
        "idb_pull_integration_realcluster_test_next_gen_1029"
    ) is None


def test_load_target_namespaces_defaults_to_ci_namespaces_for_apps_namespace(
    sqlite_engine,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CI_DASHBOARD_POD_EVENT_NAMESPACES", raising=False)
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, pod_name, start_time
                ) VALUES (
                  100, 'prow-job-ns-1', 'apps', 'job-a', 'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-a/1/',
                  'pod-a', '2099-04-20T12:00:00Z'
                )
                """
            )
        )
        namespaces = _load_target_namespaces(connection)

    assert namespaces == ["prow-test-pods", "jenkins-tidb", "jenkins-tiflow"]


def test_load_target_namespaces_uses_env_override(sqlite_engine, monkeypatch) -> None:
    monkeypatch.setenv("CI_DASHBOARD_POD_EVENT_NAMESPACES", "prow-test-pods,custom-ns")
    with sqlite_engine.begin() as connection:
        namespaces = _load_target_namespaces(connection)

    assert namespaces == ["prow-test-pods", "custom-ns"]


def test_sync_pod_helper_functions_cover_common_edge_cases(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, pod_name, start_time, normalized_build_url, build_system
                ) VALUES (
                  101, 'prow-job-extra', 'custom-ns', 'job-a', 'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-a/101/',
                  'pod-a', '2026-04-20T12:00:00Z',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-a/101/',
                  'JENKINS'
                )
                """
            )
        )
        assert _load_target_namespaces(connection) == [
            "prow-test-pods",
            "jenkins-tidb",
            "jenkins-tiflow",
            "custom-ns",
        ]
        _upsert_pod_events(connection, [])

    monkeypatch.delenv("CI_DASHBOARD_POD_SYNC_OVERLAP_MINUTES", raising=False)
    monkeypatch.delenv("CI_DASHBOARD_POD_SYNC_LOOKBACK_MINUTES", raising=False)
    assert _compute_start_from({}, datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)) == datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
    assert _compute_start_from({"last_receive_timestamp": "invalid"}, datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)) == datetime(
        2026, 4, 20, 10, 0, 0, tzinfo=UTC
    )

    monkeypatch.setenv("TEST_INT_ENV", "")
    assert _read_int_env("TEST_INT_ENV", 9) == 9
    monkeypatch.setenv("TEST_INT_ENV", "oops")
    assert _read_int_env("TEST_INT_ENV", 9) == 9
    monkeypatch.setenv("TEST_INT_ENV", "0")
    assert _read_int_env("TEST_INT_ENV", 9) == 9
    monkeypatch.setenv("TEST_INT_ENV", "12")
    assert _read_int_env("TEST_INT_ENV", 9) == 12

    assert _extract_project_from_log_name("projects/pingcap-testing-account/logs/events") == "pingcap-testing-account"
    assert _extract_project_from_log_name("bad-log-name") is None
    assert _coerce_str_mapping({"a": " 1 ", "": "bad", "b": None, 3: "x"}) == {"a": "1", "3": "x"}
    assert _json_dumps_or_none({}) is None
    assert _json_dumps_or_none({"b": "2", "a": "1"}) == '{"a":"1","b":"2"}'
    assert _format_rfc3339(datetime(2026, 4, 20, 12, 0, 0)) == "2026-04-20T12:00:00Z"
    assert _max_receive_timestamp([]) is None
    assert _null_safe_equals_sql("a", "b", "sqlite") == "a IS b"
    assert _null_safe_equals_sql("a", "b", "mysql") == "a <=> b"
    assert {"Failed", "BackOff", "ErrImagePull", "ImagePullBackOff"}.issubset(POD_EVENT_REASONS)
    relation_sql, params = _build_requested_pods_relation([("p1", "ns", "uid", "pod")])
    assert "UNION ALL" not in relation_sql
    assert params == {
        "source_project_0": "p1",
        "namespace_name_0": "ns",
        "pod_uid_0": "uid",
        "pod_name_0": "pod",
    }


def test_sync_pods_http_and_kubernetes_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _decode_json_object(b'{"ok": true}', error_context="Logging API") == {"ok": True}
    with pytest.raises(RuntimeError, match="invalid JSON"):
        _decode_json_object(b"<html>bad</html>", error_context="Logging API")
    with pytest.raises(RuntimeError, match="response is not an object"):
        _decode_json_object(b"[1,2,3]", error_context="Logging API")

    class _RetryHTTPError(urllib_error.HTTPError):
        def __init__(self, code: int, body: str) -> None:
            super().__init__("https://example.test", code, "error", hdrs=None, fp=io.BytesIO(body.encode("utf-8")))
            self._body = body.encode("utf-8")

        def read(self) -> bytes:
            return self._body

    attempts = {"count": 0}

    def fake_urlopen_retry(request, timeout=0, context=None):
        del request, timeout, context
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _RetryHTTPError(503, "retry me")
        return _FakeHTTPResponse('{"ok": true}')

    monkeypatch.setattr("ci_dashboard.jobs.sync_pods.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("ci_dashboard.jobs.sync_pods.urllib_request.urlopen", fake_urlopen_retry)
    request = __import__("urllib.request", fromlist=["Request"]).Request("https://example.test")
    assert _request_json(request, timeout=1, error_context="Logging API") == {"ok": True}
    assert attempts["count"] == 2

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods.urllib_request.urlopen",
        lambda request, timeout=0, context=None: (_ for _ in ()).throw(urllib_error.URLError("down")),
    )
    with pytest.raises(RuntimeError, match="down"):
        _request_json(request, timeout=1, error_context="Logging API")

    monkeypatch.setenv("CI_DASHBOARD_GCP_PROJECT", "pingcap-testing-account")
    monkeypatch.setenv("CI_DASHBOARD_GCP_ACCESS_TOKEN", "token")
    pages = iter(
        [
            {"entries": [{"id": 1}], "nextPageToken": "next"},
            {"entries": [{"id": 2}]},
        ]
    )
    monkeypatch.setattr("ci_dashboard.jobs.sync_pods._post_json", lambda *args, **kwargs: next(pages))
    assert _fetch_pod_event_entries(
        start_from=datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 4, 20, 13, 0, 0, tzinfo=UTC),
        namespaces=["prow-test-pods"],
    ) == [{"id": 1}, {"id": 2}]

    monkeypatch.setenv("CI_DASHBOARD_KUBERNETES_API_URL", "https://k8s.internal/")
    assert _get_kubernetes_api_url() == "https://k8s.internal"
    monkeypatch.delenv("CI_DASHBOARD_KUBERNETES_API_URL", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "6443")
    assert _get_kubernetes_api_url() == "https://10.0.0.1:6443"
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(RuntimeError, match="Unable to resolve Kubernetes API host"):
        _get_kubernetes_api_url()

    monkeypatch.setenv("CI_DASHBOARD_KUBERNETES_BEARER_TOKEN", "inline-token")
    assert _get_kubernetes_api_token() == "inline-token"
    monkeypatch.delenv("CI_DASHBOARD_KUBERNETES_BEARER_TOKEN", raising=False)
    monkeypatch.setattr("ci_dashboard.jobs.sync_pods._read_text_file", lambda path: "file-token")
    assert _get_kubernetes_api_token() == "file-token"
    monkeypatch.setattr("ci_dashboard.jobs.sync_pods._read_text_file", lambda path: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(RuntimeError, match="Unable to read Kubernetes service account token"):
        _get_kubernetes_api_token()

    monkeypatch.setenv("CI_DASHBOARD_KUBERNETES_CA_FILE", "/tmp/ca.pem")
    assert _get_kubernetes_api_ca_file() == "/tmp/ca.pem"
    monkeypatch.delenv("CI_DASHBOARD_KUBERNETES_CA_FILE", raising=False)
    monkeypatch.setattr("ci_dashboard.jobs.sync_pods.os.path.exists", lambda path: False)
    assert _get_kubernetes_api_ca_file() is None


def test_sync_pods_build_matching_helpers_cover_jenkins_paths(sqlite_engine, monkeypatch) -> None:
    assert _normalize_jenkins_runtime_url("https://do.pingcap.net/jenkins/job/a/1/") is None
    assert _normalize_jenkins_runtime_url("https://prow.tidb.net/jenkins/job/a/1/") == "https://prow.tidb.net/jenkins/job/a/1/"
    assert _infer_pod_build_system("jenkins-tidb") == "JENKINS"
    assert _infer_pod_build_system("prow-test-pods") == "PROW_NATIVE"
    assert _infer_pod_build_system("apps") == "UNKNOWN"

    assert _parse_jenkins_pod_name_build_ref("jenkins-agent-1413-abcd") == JenkinsPodNameBuildRef(
        pod_prefix="jenkins-agent",
        build_number="1413",
    )
    assert _parse_jenkins_pod_name_build_ref("jenkins-agent-a-b") is None
    assert _extract_normalized_build_url_from_pod_name(
        "jenkins-agent-1413-abcd",
        {"jenkins-agent": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test/"},
    ) == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test/1413/"

    metadata = _pod_metadata(
        annotations={"buildUrl": "http://jenkins.jenkins.svc.cluster.local/job/pingcap/job/tidb/job/test/1413/"},
    )
    assert (
        _extract_normalized_build_url_from_metadata(metadata)
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test/1413/"
    )
    metadata_from_label = _pod_metadata(
        labels={"org": "pingcap", "repo": "tidb", "jenkins/label": "ghpr-unit-test_1413-abcd"},
        annotations={"ci_job": "pingcap/tidb/ghpr_unit_test"},
    )
    assert (
        _extract_normalized_build_url_from_metadata(metadata_from_label)
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413/"
    )
    assert _build_jenkins_url_from_label_and_ci_job(org=None, repo="tidb", ci_job="pingcap/tidb/job", jenkins_label="x_1-a") is None
    assert _build_jenkins_url_from_label_and_ci_job(org="pingcap", repo="tidb", ci_job="other/job", jenkins_label="x_1-a") is None
    assert _build_jenkins_url_from_label_and_ci_job(org="pingcap", repo="tidb", ci_job="pingcap/tidb/", jenkins_label="x_1-a") is None
    assert _extract_build_number_from_jenkins_label(None) is None
    assert _extract_build_number_from_jenkins_label("only-one-segment") is None

    assert _build_ci_job_from_build_metadata({"job_name": "ghpr_unit_test", "repo_full_name": "pingcap/tidb"}) == "pingcap/tidb/ghpr_unit_test"
    assert _build_ci_job_from_build_metadata({"job_name": "pingcap/tidb/ghpr_unit_test"}) == "pingcap/tidb/ghpr_unit_test"
    assert _build_ci_job_from_build_metadata({"job_name": "ghpr_unit_test"}) is None

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, pod_name, start_time, normalized_build_url, build_system
                ) VALUES
                (
                  201, 'prow-job-201', 'jenkins-tidb', 'test-a', 'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1413/',
                  'jenkins-agent-1413-abcd', '2026-04-20T12:00:00Z',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1413/',
                  'JENKINS'
                ),
                (
                  202, 'prow-job-202', 'jenkins-tidb', 'test-a', 'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1414/',
                  'jenkins-agent-1413-abcd', '2026-04-19T12:00:00Z',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1414/',
                  'JENKINS'
                )
                """
            )
        )
        prefix_map = _load_jenkins_pod_name_url_prefix_map(connection)

    assert prefix_map["jenkins-agent"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/"
    resolved = _resolve_jenkins_build_metadata(
        scheduled_at=datetime(2026, 4, 20, 12, 1, 0),
        candidate_urls=[
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1413/",
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/9999/",
        ],
        build_candidates_by_url={
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1413/": [
                {
                    "source_prow_job_id": "prow-job-201",
                    "org": "pingcap",
                    "repo": "tidb",
                    "repo_full_name": "pingcap/tidb",
                    "job_name": "test-a",
                    "author": "alice",
                    "start_time": "2026-04-20T12:00:00Z",
                }
            ]
        },
    )
    assert resolved["source_prow_job_id"] == "prow-job-201"
    assert _resolve_jenkins_build_metadata(
        scheduled_at=None,
        candidate_urls=["https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1413/"],
        build_candidates_by_url={},
    ) == {
        "build_system": "JENKINS",
        "normalized_build_url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/test-a/1413/",
    }


def test_sync_pods_metadata_fetch_and_logging_entry_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _normalize_logging_entry({"logName": "projects/a/logs/events"}) is None
    assert _normalize_logging_entry({"insertId": "1", "logName": "projects/a/logs/events", "jsonPayload": {}}) is None
    normalized = _normalize_logging_entry(
        {
            "insertId": "ins-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "receiveTimestamp": "2026-04-20T12:53:54Z",
            "resource": {"labels": {"namespace_name": "prow-test-pods", "pod_name": "pod-a"}},
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "assigned",
                "involvedObject": {"uid": "uid-a"},
                "lastTimestamp": "2026-04-20T12:53:49Z",
            },
        }
    )
    assert normalized is not None
    assert normalized.source_project == "pingcap-testing-account"
    assert normalized.event_timestamp == datetime(2026, 4, 20, 12, 53, 49)

    observed_at = datetime(2026, 4, 20, 13, 0, 0)
    pages = iter(
        [
            {
                "items": [
                    {
                        "metadata": {
                            "name": "pod-a",
                            "uid": "uid-a",
                            "creationTimestamp": "2026-04-20T12:49:30Z",
                            "labels": {"author": "alice"},
                            "annotations": {"ci_job": "pingcap/tidb/test-a"},
                        }
                    },
                    "ignore-me",
                ],
                "metadata": {"continue": "next"},
            },
            {"items": [], "metadata": {}},
        ]
    )
    monkeypatch.setattr("ci_dashboard.jobs.sync_pods._get_json", lambda *args, **kwargs: next(pages))
    snapshots = _list_namespace_pod_metadata(
        namespace_name="jenkins-tidb",
        requested_pod_names={"pod-a"},
        base_url="https://k8s.internal",
        token="token",
        ca_file=None,
        observed_at=observed_at,
    )
    assert snapshots["pod-a"].pod_uid == "uid-a"
    assert snapshots["pod-a"].labels == {"author": "alice"}
    assert snapshots["pod-a"].annotations == {"ci_job": "pingcap/tidb/test-a"}
    assert snapshots["pod-a"].creation_timestamp == datetime(2026, 4, 20, 12, 49, 30)


def test_sync_pods_end_to_end_and_idempotent(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  1, 'prow-job-1', 'prow-test-pods', 'ghpr_unit_test', 'presubmit', 'failure',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/66206/ghpr_unit_test/1/',
                  'https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/66206/ghpr_unit_test/1/',
                  'pod-abc', '2026-04-20T12:50:00Z', 'GCP', 'PROW_NATIVE'
                )
                """
            )
        )

    entries = [
        {
            "insertId": "ins-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-20T12:53:49Z",
            "receiveTimestamp": "2026-04-20T12:53:54Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "prow-test-pods",
                    "pod_name": "pod-abc",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-abc"},
                "firstTimestamp": "2026-04-20T12:53:49Z",
                "lastTimestamp": "2026-04-20T12:53:49Z",
            },
        },
        {
            "insertId": "ins-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-20T12:53:51Z",
            "receiveTimestamp": "2026-04-20T12:53:55Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "prow-test-pods",
                    "pod_name": "pod-abc",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-abc"},
                "firstTimestamp": "2026-04-20T12:53:51Z",
                "lastTimestamp": "2026-04-20T12:53:51Z",
            },
        },
        {
            "insertId": "ins-3",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-20T12:53:48Z",
            "receiveTimestamp": "2026-04-20T12:53:54Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "prow-test-pods",
                    "pod_name": "pod-abc",
                }
            },
            "jsonPayload": {
                "reason": "FailedScheduling",
                "type": "Warning",
                "message": "0/10 nodes are available",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-abc"},
                "firstTimestamp": "2026-04-20T12:53:48Z",
                "lastTimestamp": "2026-04-20T12:53:48Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )

    first = run_sync_pods(sqlite_engine, _settings(batch_size=2))
    assert first.source_rows_scanned == 3
    assert first.event_rows_written == 3
    assert first.pods_touched == 1
    assert first.lifecycle_rows_upserted == 1
    assert first.last_receive_timestamp == "2026-04-20T12:53:55Z"

    second = run_sync_pods(sqlite_engine, _settings(batch_size=2))
    assert second.source_rows_scanned == 3
    assert second.event_rows_written == 3
    assert second.pods_touched == 1

    with sqlite_engine.begin() as connection:
        event_count = connection.execute(
            text("SELECT COUNT(*) AS count FROM ci_l1_pod_events")
        ).mappings().one()["count"]
        lifecycle = connection.execute(
            text(
                """
                SELECT
                  build_system,
                  source_prow_job_id,
                  normalized_build_url,
                  repo_full_name,
                  job_name,
                  scheduled_at,
                  first_started_at,
                  failed_scheduling_count,
                  schedule_to_started_seconds
                FROM ci_l1_pod_lifecycle
                WHERE source_project = 'pingcap-testing-account'
                  AND namespace_name = 'prow-test-pods'
                  AND pod_uid = 'uid-abc'
                """
            )
        ).mappings().one()
        state = get_job_state(connection, "ci-sync-pods")

    assert event_count == 3
    assert lifecycle["build_system"] == "PROW_NATIVE"
    assert lifecycle["source_prow_job_id"] == "prow-job-1"
    assert lifecycle["normalized_build_url"] == "https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/66206/ghpr_unit_test/1/"
    assert lifecycle["repo_full_name"] == "pingcap/tidb"
    assert lifecycle["job_name"] == "pingcap/tidb/ghpr_unit_test"
    assert str(lifecycle["scheduled_at"]).startswith("2026-04-20 12:53:49")
    assert str(lifecycle["first_started_at"]).startswith("2026-04-20 12:53:51")
    assert lifecycle["failed_scheduling_count"] == 1
    assert lifecycle["schedule_to_started_seconds"] == 2
    assert state is not None
    assert state.last_status == "succeeded"
    assert state.watermark["last_receive_timestamp"] == "2026-04-20T12:53:55Z"


def test_sync_pods_links_jenkins_pods_from_annotations_and_preserves_fanout(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  2, 'prow-job-jenkins-1413', 'apps', 'ghpr_unit_test', 'presubmit', 'failure',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413/',
                  '93d66f6a-25c1-4431-987e-f86351d415c0', '2026-04-20T12:50:00Z', 'GCP', 'JENKINS'
                )
                """
            )
        )

    entries = [
        {
            "insertId": "jenkins-ins-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-20T12:53:49Z",
            "receiveTimestamp": "2026-04-20T12:53:54Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "opaque-agent-pod-a",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-j-1"},
                "firstTimestamp": "2026-04-20T12:53:49Z",
                "lastTimestamp": "2026-04-20T12:53:49Z",
            },
        },
        {
            "insertId": "jenkins-ins-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-20T12:54:05Z",
            "receiveTimestamp": "2026-04-20T12:54:06Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "opaque-agent-pod-a",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-j-1"},
                "firstTimestamp": "2026-04-20T12:54:05Z",
                "lastTimestamp": "2026-04-20T12:54:05Z",
            },
        },
        {
            "insertId": "jenkins-ins-3",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-20T12:54:10Z",
            "receiveTimestamp": "2026-04-20T12:54:11Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "opaque-agent-pod-b",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-2",
                "involvedObject": {"uid": "uid-j-2"},
                "firstTimestamp": "2026-04-20T12:54:10Z",
                "lastTimestamp": "2026-04-20T12:54:10Z",
            },
        },
        {
            "insertId": "jenkins-ins-4",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-20T12:54:30Z",
            "receiveTimestamp": "2026-04-20T12:54:31Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "opaque-agent-pod-b",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-2",
                "involvedObject": {"uid": "uid-j-2"},
                "firstTimestamp": "2026-04-20T12:54:30Z",
                "lastTimestamp": "2026-04-20T12:54:30Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_pod_metadata_snapshots",
        lambda _pods: {
            _pod_identity("jenkins-tidb", "uid-j-1", "opaque-agent-pod-a"): _pod_metadata(
                pod_uid="uid-j-1",
                creation_timestamp=datetime(2026, 4, 20, 12, 52, 10),
                labels={
                    "author": "tester-a",
                    "org": "pingcap",
                    "repo": "tidb",
                    "jenkins/label": "pingcap_tidb_ghpr_unit_test_1413-6d2xf",
                    "jenkins/label-digest": "digest-a",
                    "kubernetes.jenkins.io/controller": "http___jenkins_jenkins_svc_cluster_local_80_jenkinsx",
                },
                annotations={
                    "buildUrl": "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413/",
                    "runUrl": "job/pingcap/job/tidb/job/ghpr_unit_test/1413/",
                    "ci_job": "pingcap/tidb/ghpr_unit_test",
                },
            ),
            _pod_identity("jenkins-tidb", "uid-j-2", "opaque-agent-pod-b"): _pod_metadata(
                pod_uid="uid-j-2",
                creation_timestamp=datetime(2026, 4, 20, 12, 52, 20),
                labels={
                    "author": "tester-b",
                    "org": "pingcap",
                    "repo": "tidb",
                    "jenkins/label": "pingcap_tidb_ghpr_unit_test_1413-abcde",
                    "jenkins/label-digest": "digest-b",
                    "kubernetes.jenkins.io/controller": "http___jenkins_jenkins_svc_cluster_local_80_jenkinsx",
                },
                annotations={
                    "buildUrl": "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413/",
                    "runUrl": "job/pingcap/job/tidb/job/ghpr_unit_test/1413/",
                    "ci_job": "pingcap/tidb/ghpr_unit_test",
                },
            ),
        },
    )

    summary = run_sync_pods(sqlite_engine, _settings(batch_size=2))

    assert summary.source_rows_scanned == 4
    assert summary.event_rows_written == 4
    assert summary.pods_touched == 2
    assert summary.lifecycle_rows_upserted == 2

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT
                      build_system,
                      pod_author,
                      pod_org,
                      pod_repo,
                      jenkins_label,
                      ci_job,
                      pod_created_at,
                      source_prow_job_id,
                      normalized_build_url,
                      repo_full_name,
                      job_name,
                      pod_name,
                      schedule_to_started_seconds
                    FROM ci_l1_pod_lifecycle
                    WHERE namespace_name = 'jenkins-tidb'
                    ORDER BY pod_name
                    """
                )
            ).mappings()
        )

    assert len(rows) == 2
    assert {row["build_system"] for row in rows} == {"JENKINS"}
    assert {row["pod_author"] for row in rows} == {"tester-a", "tester-b"}
    assert {row["pod_org"] for row in rows} == {"pingcap"}
    assert {row["pod_repo"] for row in rows} == {"tidb"}
    assert {row["ci_job"] for row in rows} == {"pingcap/tidb/ghpr_unit_test"}
    assert {row["source_prow_job_id"] for row in rows} == {"prow-job-jenkins-1413"}
    assert {row["normalized_build_url"] for row in rows} == {
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413/"
    }
    assert {row["repo_full_name"] for row in rows} == {"pingcap/tidb"}
    assert {row["job_name"] for row in rows} == {"pingcap/tidb/ghpr_unit_test"}
    assert {str(row["pod_created_at"]) for row in rows} == {
        "2026-04-20 12:52:10",
        "2026-04-20 12:52:20",
    }
    assert {row["schedule_to_started_seconds"] for row in rows} == {16, 20}


def test_sync_pods_links_jenkins_pod_from_build_url_annotation_with_opaque_name(
    sqlite_engine,
    monkeypatch,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  3, 'prow-job-jenkins-299', 'apps',
                  'pull_dm_integration_test',
                  'presubmit', 'failure',
                  0, 1, 'pingcap', 'tiflow', 'pingcap/tiflow',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/pull_dm_integration_test/299/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/pull_dm_integration_test/299/',
                  'e6bac7fe-8bc3-4829-b19f-114f99a6e6da', '2026-04-21T08:14:17Z', 'GCP', 'JENKINS'
                )
                """
            )
        )

    entries = [
        {
            "insertId": "jenkins-mid-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T08:14:20Z",
            "receiveTimestamp": "2026-04-21T08:14:24Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tiflow",
                    "pod_name": "dm-it-d1ec79f0-0187-4fe1-abf0-7e974163cc54-24mww-n1tzj",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-mid-1"},
                "firstTimestamp": "2026-04-21T08:14:20Z",
                "lastTimestamp": "2026-04-21T08:14:20Z",
            },
        },
        {
            "insertId": "jenkins-mid-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T08:14:40Z",
            "receiveTimestamp": "2026-04-21T08:14:44Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tiflow",
                    "pod_name": "dm-it-d1ec79f0-0187-4fe1-abf0-7e974163cc54-24mww-n1tzj",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-mid-1"},
                "firstTimestamp": "2026-04-21T08:14:40Z",
                "lastTimestamp": "2026-04-21T08:14:40Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_pod_metadata_snapshots",
        lambda _pods: {
            _pod_identity(
                "jenkins-tiflow",
                "uid-mid-1",
                "dm-it-d1ec79f0-0187-4fe1-abf0-7e974163cc54-24mww-n1tzj",
            ): _pod_metadata(
                pod_uid="uid-mid-1",
                labels={
                    "author": "joechenrh",
                    "org": "pingcap",
                    "repo": "tiflow",
                    "jenkins/label": "dm-it-14d6b05a-f21b-498e-a4fc-701d061341a1",
                    "jenkins/label-digest": "480caabc263d4f660716bb5a31d310615c2c5f5a",
                    "kubernetes.jenkins.io/controller": "http___jenkins_jenkins_svc_cluster_local_80_jenkinsx",
                },
                annotations={
                    "buildUrl": "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tiflow/job/pull_dm_integration_test/299/",
                    "runUrl": "job/pingcap/job/tiflow/job/pull_dm_integration_test/299/",
                    "ci_job": "pingcap/tiflow/pull_dm_integration_test",
                },
            ),
        },
    )

    run_sync_pods(sqlite_engine, _settings(batch_size=2))

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT source_prow_job_id, normalized_build_url, repo_full_name, job_name, pod_annotations_json
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-mid-1'
                """
            )
        ).mappings().one()

    assert row["source_prow_job_id"] == "prow-job-jenkins-299"
    assert row["normalized_build_url"] == (
        "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/pull_dm_integration_test/299/"
    )
    assert row["repo_full_name"] == "pingcap/tiflow"
    assert row["job_name"] == "pingcap/tiflow/pull_dm_integration_test"
    assert "buildUrl" in row["pod_annotations_json"]


def test_sync_pods_supplements_sparse_jenkins_runtime_metadata_from_build(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  author, pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  31, 'prow-job-jenkins-supplement-1718', 'apps',
                  'pull_integration_realcluster_test_next_gen',
                  'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1718/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1718/',
                  'teru01',
                  '93d66f6a-25c1-4431-987e-f86351d415c0', '2026-04-22T12:50:00Z', 'GCP', 'JENKINS'
                )
                """
            )
        )

    entries = [
        {
            "insertId": "jenkins-supplement-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-22T12:53:49Z",
            "receiveTimestamp": "2026-04-22T12:53:54Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "opaque-runtime-agent-pod",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-supplement-1"},
                "firstTimestamp": "2026-04-22T12:53:49Z",
                "lastTimestamp": "2026-04-22T12:53:49Z",
            },
        },
        {
            "insertId": "jenkins-supplement-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-22T12:54:05Z",
            "receiveTimestamp": "2026-04-22T12:54:06Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "opaque-runtime-agent-pod",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-supplement-1"},
                "firstTimestamp": "2026-04-22T12:54:05Z",
                "lastTimestamp": "2026-04-22T12:54:05Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_pod_metadata_snapshots",
        lambda _pods: {
            _pod_identity("jenkins-tidb", "uid-supplement-1", "opaque-runtime-agent-pod"): _pod_metadata(
                pod_uid="uid-supplement-1",
                labels={
                    "jenkins/label": "idb_pull_integration_realcluster_test_next_gen_1718-kz725",
                    "jenkins/label-digest": "digest-supplement",
                    "kubernetes.jenkins.io/controller": "http___jenkins_jenkins_svc_cluster_local_80_jenkinsx",
                },
                annotations={
                    "buildUrl": "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1718/",
                    "runUrl": "job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1718/",
                },
            ),
        },
    )

    run_sync_pods(sqlite_engine, _settings(batch_size=2))

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT
                  pod_author,
                  pod_org,
                  pod_repo,
                  ci_job,
                  source_prow_job_id,
                  normalized_build_url
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-supplement-1'
                """
            )
        ).mappings().one()

    assert row["pod_author"] == "teru01"
    assert row["pod_org"] == "pingcap"
    assert row["pod_repo"] == "tidb"
    assert row["ci_job"] == "pingcap/tidb/pull_integration_realcluster_test_next_gen"
    assert row["source_prow_job_id"] == "prow-job-jenkins-supplement-1718"
    assert row["normalized_build_url"] == (
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1718/"
    )


def test_sync_pods_links_jenkins_pod_from_label_and_ci_job_fallback(
    sqlite_engine,
    monkeypatch,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  4, 'prow-job-jenkins-1029', 'apps',
                  'pull_integration_realcluster_test_next_gen',
                  'presubmit', 'failure',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1029/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1029/',
                  '80417655-9361-46e7-95eb-52d88c3875c2', '2026-04-02T02:50:29Z', 'GCP', 'JENKINS'
                )
                """
            )
        )

    entries = [
        {
            "insertId": "jenkins-raw-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-02T02:50:31Z",
            "receiveTimestamp": "2026-04-02T02:50:35Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "completely-opaque-runtime-pod",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-raw-1"},
                "firstTimestamp": "2026-04-02T02:50:31Z",
                "lastTimestamp": "2026-04-02T02:50:31Z",
            },
        },
        {
            "insertId": "jenkins-raw-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-02T02:50:51Z",
            "receiveTimestamp": "2026-04-02T02:50:55Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "completely-opaque-runtime-pod",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-raw-1"},
                "firstTimestamp": "2026-04-02T02:50:51Z",
                "lastTimestamp": "2026-04-02T02:50:51Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_pod_metadata_snapshots",
        lambda _pods: {
            _pod_identity("jenkins-tidb", "uid-raw-1", "completely-opaque-runtime-pod"): _pod_metadata(
                pod_uid="uid-raw-1",
                labels={
                    "author": "flaky-claw",
                    "org": "pingcap",
                    "repo": "tidb",
                    "jenkins/label": "idb_pull_integration_realcluster_test_next_gen_1029-kz725",
                    "jenkins/label-digest": "2412a13b0cff2a4498ec8cc0b9e15cc91dfaf0dc",
                    "kubernetes.jenkins.io/controller": "http___jenkins_jenkins_svc_cluster_local_80_jenkinsx",
                },
                annotations={
                    "ci_job": "pingcap/tidb/pull_integration_realcluster_test_next_gen",
                },
            ),
        },
    )

    run_sync_pods(sqlite_engine, _settings(batch_size=2))

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT source_prow_job_id, normalized_build_url, repo_full_name, job_name
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-raw-1'
                """
            )
        ).mappings().one()

    assert row["source_prow_job_id"] == "prow-job-jenkins-1029"
    assert row["normalized_build_url"] == (
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1029/"
    )
    assert row["repo_full_name"] == "pingcap/tidb"
    assert row["job_name"] == "pingcap/tidb/pull_integration_realcluster_test_next_gen"


def test_sync_pods_keeps_jenkins_pod_without_annotation_or_label_build_key_unresolved(
    sqlite_engine,
    monkeypatch,
) -> None:
    entries = [
        {
            "insertId": "jenkins-amb-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-03T01:02:00Z",
            "receiveTimestamp": "2026-04-03T01:02:05Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "dm-it-opaque-no-key",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-amb-1"},
                "firstTimestamp": "2026-04-03T01:02:00Z",
                "lastTimestamp": "2026-04-03T01:02:00Z",
            },
        },
        {
            "insertId": "jenkins-amb-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-03T01:02:20Z",
            "receiveTimestamp": "2026-04-03T01:02:25Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "dm-it-opaque-no-key",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-amb-1"},
                "firstTimestamp": "2026-04-03T01:02:20Z",
                "lastTimestamp": "2026-04-03T01:02:20Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_pod_metadata_snapshots",
        lambda _pods: {
            _pod_identity("jenkins-tidb", "uid-amb-1", "dm-it-opaque-no-key"): _pod_metadata(
                pod_uid="uid-amb-1",
                labels={
                    "author": "unknown",
                    "org": "pingcap",
                    "repo": "tidb",
                    "jenkins/label": "dm-it-14d6b05a-f21b-498e-a4fc-701d061341a1",
                },
                annotations={
                    "ci_job": "pingcap/tidb/pull_dm_integration_test",
                },
            ),
        },
    )

    run_sync_pods(sqlite_engine, _settings(batch_size=2))

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT build_system, source_prow_job_id, normalized_build_url, pod_labels_json, ci_job
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-amb-1'
                """
            )
        ).mappings().one()

    assert row["build_system"] == "JENKINS"
    assert row["source_prow_job_id"] is None
    assert row["normalized_build_url"] is None
    assert "dm-it-14d6b05a-f21b-498e-a4fc-701d061341a1" in row["pod_labels_json"]
    assert row["ci_job"] == "pingcap/tidb/pull_dm_integration_test"


def test_sync_pods_links_jenkins_pod_from_pod_name_parse_when_live_metadata_is_missing(
    sqlite_engine,
    monkeypatch,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  5, 'prow-job-jenkins-2048', 'apps',
                  'pull_integration_realcluster_test_next_gen',
                  'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2048/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2048/',
                  'tidb-ci-pull-integration-realcluster-test-next-gen-2048-abcd3', '2026-04-21T10:14:17Z', 'GCP', 'JENKINS'
                )
                """
            )
        )

    entries = [
        {
            "insertId": "jenkins-pod-name-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T10:14:20Z",
            "receiveTimestamp": "2026-04-21T10:14:24Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "tidb-ci-pull-integration-realcluster-test-next-gen-2048-z9k2m",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-pod-name-1"},
                "firstTimestamp": "2026-04-21T10:14:20Z",
                "lastTimestamp": "2026-04-21T10:14:20Z",
            },
        },
        {
            "insertId": "jenkins-pod-name-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T10:14:40Z",
            "receiveTimestamp": "2026-04-21T10:14:44Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "tidb-ci-pull-integration-realcluster-test-next-gen-2048-z9k2m",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-pod-name-1"},
                "firstTimestamp": "2026-04-21T10:14:40Z",
                "lastTimestamp": "2026-04-21T10:14:40Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_pod_metadata_snapshots",
        lambda _pods: {},
    )

    run_sync_pods(sqlite_engine, _settings(batch_size=2))

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT source_prow_job_id, normalized_build_url, repo_full_name, job_name
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-pod-name-1'
                """
            )
        ).mappings().one()

    assert row["source_prow_job_id"] == "prow-job-jenkins-2048"
    assert row["normalized_build_url"] == (
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2048/"
    )
    assert row["repo_full_name"] == "pingcap/tidb"
    assert row["job_name"] == "pingcap/tidb/pull_integration_realcluster_test_next_gen"


def test_load_jenkins_pod_name_url_prefix_map_ignores_old_builds(sqlite_engine, monkeypatch) -> None:
    monkeypatch.setenv("CI_DASHBOARD_JENKINS_POD_NAME_PREFIX_LOOKBACK_DAYS", "30")

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  7, 'prow-job-jenkins-old-1', 'apps',
                  'pull_integration_realcluster_test_next_gen',
                  'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1030/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1030/',
                  'tidb-ci-pull-integration-realcluster-test-next-gen-1030-abcde', '2000-01-01 00:00:00', 'GCP', 'JENKINS'
                )
                """
            )
        )

        assert _load_jenkins_pod_name_url_prefix_map(connection) == {}


def test_sync_pods_loads_jenkins_prefix_map_once_per_run(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES
                  (
                    8, 'prow-job-jenkins-2050', 'apps',
                    'pull_integration_realcluster_test_next_gen',
                    'presubmit', 'success',
                    0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2050/',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2050/',
                    'tidb-ci-pull-integration-realcluster-test-next-gen-2050-oldaa', '2026-04-21T12:14:18Z', 'GCP', 'JENKINS'
                  ),
                  (
                    9, 'prow-job-jenkins-2051', 'apps',
                    'pull_integration_realcluster_test_next_gen',
                    'presubmit', 'success',
                    0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2051/',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2051/',
                    'tidb-ci-pull-integration-realcluster-test-next-gen-2051-oldbb', '2026-04-21T12:15:18Z', 'GCP', 'JENKINS'
                  )
                """
            )
        )

    entries = [
        {
            "insertId": "jenkins-cache-1",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T12:14:20Z",
            "receiveTimestamp": "2026-04-21T12:14:24Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "tidb-ci-pull-integration-realcluster-test-next-gen-2050-z9k2m",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-cache-1"},
                "firstTimestamp": "2026-04-21T12:14:20Z",
                "lastTimestamp": "2026-04-21T12:14:20Z",
            },
        },
        {
            "insertId": "jenkins-cache-2",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T12:14:40Z",
            "receiveTimestamp": "2026-04-21T12:14:44Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "tidb-ci-pull-integration-realcluster-test-next-gen-2050-z9k2m",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-cache-1"},
                "firstTimestamp": "2026-04-21T12:14:40Z",
                "lastTimestamp": "2026-04-21T12:14:40Z",
            },
        },
        {
            "insertId": "jenkins-cache-3",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T12:15:20Z",
            "receiveTimestamp": "2026-04-21T12:15:24Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "tidb-ci-pull-integration-realcluster-test-next-gen-2051-x8y7z",
                }
            },
            "jsonPayload": {
                "reason": "Scheduled",
                "type": "Normal",
                "message": "Successfully assigned",
                "reportingComponent": "default-scheduler",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-cache-2"},
                "firstTimestamp": "2026-04-21T12:15:20Z",
                "lastTimestamp": "2026-04-21T12:15:20Z",
            },
        },
        {
            "insertId": "jenkins-cache-4",
            "logName": "projects/pingcap-testing-account/logs/events",
            "timestamp": "2026-04-21T12:15:40Z",
            "receiveTimestamp": "2026-04-21T12:15:44Z",
            "resource": {
                "labels": {
                    "cluster_name": "prow",
                    "location": "us-central1-c",
                    "namespace_name": "jenkins-tidb",
                    "pod_name": "tidb-ci-pull-integration-realcluster-test-next-gen-2051-x8y7z",
                }
            },
            "jsonPayload": {
                "reason": "Started",
                "type": "Normal",
                "message": "Started container test",
                "reportingComponent": "kubelet",
                "reportingInstance": "gke-node-1",
                "involvedObject": {"uid": "uid-cache-2"},
                "firstTimestamp": "2026-04-21T12:15:40Z",
                "lastTimestamp": "2026-04-21T12:15:40Z",
            },
        },
    ]

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._fetch_pod_event_entries",
        lambda **_: entries,
    )
    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_pod_metadata_snapshots",
        lambda _pods: {},
    )

    calls = {"count": 0}

    def fake_load_jenkins_pod_name_url_prefix_map(_connection):
        calls["count"] += 1
        return {
            "tidb-ci-pull-integration-realcluster-test-next-gen": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/"
            )
        }

    monkeypatch.setattr(
        "ci_dashboard.jobs.sync_pods._load_jenkins_pod_name_url_prefix_map",
        fake_load_jenkins_pod_name_url_prefix_map,
    )

    run_sync_pods(sqlite_engine, _settings(batch_size=1))

    assert calls["count"] == 1


def test_reconcile_pod_linkage_range_backfills_rows_after_build_arrives(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_lifecycle (
                  source_project, cluster_name, location, namespace_name, pod_name, pod_uid,
                  build_system, scheduled_at, first_started_at, last_event_at
                ) VALUES (
                  'pingcap-testing-account', 'prow', 'us-central1-c', 'jenkins-tidb',
                  'tidb-ci-pull-integration-realcluster-test-next-gen-2049-abcde', 'uid-reconcile-1',
                  'JENKINS', '2026-04-21 11:14:20', '2026-04-21 11:14:40', '2026-04-21 11:14:40'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  6, 'prow-job-jenkins-2049', 'apps',
                  'pull_integration_realcluster_test_next_gen',
                  'presubmit', 'success',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2049/',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2049/',
                  'tidb-ci-pull-integration-realcluster-test-next-gen-2049-qwert', '2026-04-21T11:14:18Z', 'GCP', 'JENKINS'
                )
                """
            )
        )

    summary = run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        start_time_from=datetime(2026, 4, 21, 0, 0, 0),
        start_time_to=datetime(2026, 4, 22, 0, 0, 0),
    )

    assert summary.reconciled_rows_updated == 1

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT source_prow_job_id, normalized_build_url, repo_full_name, job_name
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-reconcile-1'
                """
            )
        ).mappings().one()

    assert row["source_prow_job_id"] == "prow-job-jenkins-2049"
    assert row["normalized_build_url"] == (
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2049/"
    )
    assert row["repo_full_name"] == "pingcap/tidb"
    assert row["job_name"] == "pingcap/tidb/pull_integration_realcluster_test_next_gen"


def test_reconcile_pod_linkage_range_supplements_missing_jenkins_pod_fields(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_lifecycle (
                  source_project, cluster_name, location, namespace_name, pod_name, pod_uid,
                  build_system, pod_author, pod_org, pod_repo, ci_job,
                  source_prow_job_id, normalized_build_url, repo_full_name, job_name,
                  scheduled_at, first_started_at, last_event_at
                ) VALUES
                  (
                    'pingcap-testing-account', 'prow', 'us-central1-c', 'jenkins-tidb',
                    'opaque-runtime-agent-pod-a', 'uid-reconcile-supplement-a',
                    'JENKINS', NULL, NULL, NULL, NULL,
                    'prow-job-jenkins-supplement-a',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_ddlv1/1510/',
                    'pingcap/tidb', 'pull_unit_test_ddlv1',
                    '2026-04-21 13:14:20', '2026-04-21 13:14:40', '2026-04-21 13:14:40'
                  ),
                  (
                    'pingcap-testing-account', 'prow', 'us-central1-c', 'jenkins-tidb',
                    'opaque-runtime-agent-pod-b', 'uid-reconcile-supplement-b',
                    'JENKINS', NULL, 'pingcap', 'tidb', 'pingcap/tidb/pull_build_next_gen',
                    'prow-job-jenkins-supplement-b',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/1601/',
                    'pingcap/tidb', 'pull_build_next_gen',
                    '2026-04-21 14:14:20', '2026-04-21 14:14:40', '2026-04-21 14:14:40'
                  )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  author, pod_name, start_time, cloud_phase, build_system
                ) VALUES
                  (
                    32, 'prow-job-jenkins-supplement-a', 'apps',
                    'pull_unit_test_ddlv1',
                    'presubmit', 'success',
                    0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_ddlv1/1510/',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_ddlv1/1510/',
                    'terry1purcell',
                    'opaque-runtime-agent-build-a', '2026-04-21T13:14:18Z', 'GCP', 'JENKINS'
                  ),
                  (
                    33, 'prow-job-jenkins-supplement-b', 'apps',
                    'pull_build_next_gen',
                    'presubmit', 'success',
                    0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/1601/',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/1601/',
                    'dveeden',
                    'opaque-runtime-agent-build-b', '2026-04-21T14:14:18Z', 'GCP', 'JENKINS'
                  )
                """
            )
        )

    summary = run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        start_time_from=datetime(2026, 4, 21, 0, 0, 0),
        start_time_to=datetime(2026, 4, 22, 0, 0, 0),
    )

    assert summary.reconciled_rows_updated == 2

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT pod_uid, pod_author, pod_org, pod_repo, ci_job
                    FROM ci_l1_pod_lifecycle
                    WHERE pod_uid IN ('uid-reconcile-supplement-a', 'uid-reconcile-supplement-b')
                    ORDER BY pod_uid
                    """
                )
            ).mappings()
        )

    assert rows == [
        {
            "pod_uid": "uid-reconcile-supplement-a",
            "pod_author": "terry1purcell",
            "pod_org": "pingcap",
            "pod_repo": "tidb",
            "ci_job": "pingcap/tidb/pull_unit_test_ddlv1",
        },
        {
            "pod_uid": "uid-reconcile-supplement-b",
            "pod_author": "dveeden",
            "pod_org": "pingcap",
            "pod_repo": "tidb",
            "ci_job": "pingcap/tidb/pull_build_next_gen",
        },
    ]


def test_reconcile_pod_linkage_range_processes_multiple_batches(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_lifecycle (
                  source_project, cluster_name, location, namespace_name, pod_name, pod_uid,
                  build_system, scheduled_at, first_started_at, last_event_at
                ) VALUES
                  (
                    'pingcap-testing-account', 'prow', 'us-central1-c', 'jenkins-tidb',
                    'tidb-ci-pull-integration-realcluster-test-next-gen-2052-abcde', 'uid-reconcile-b1',
                    'JENKINS', '2026-04-21 12:14:20', '2026-04-21 12:14:40', '2026-04-21 12:14:40'
                  ),
                  (
                    'pingcap-testing-account', 'prow', 'us-central1-c', 'jenkins-tidb',
                    'tidb-ci-pull-integration-realcluster-test-next-gen-2053-fghij', 'uid-reconcile-b2',
                    'JENKINS', '2026-04-21 12:15:20', '2026-04-21 12:15:40', '2026-04-21 12:15:40'
                  )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_url,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES
                  (
                    10, 'prow-job-jenkins-2052', 'apps',
                    'pull_integration_realcluster_test_next_gen',
                    'presubmit', 'success',
                    0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2052/',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2052/',
                    'tidb-ci-pull-integration-realcluster-test-next-gen-2052-qwert', '2026-04-21T12:14:18Z', 'GCP', 'JENKINS'
                  ),
                  (
                    11, 'prow-job-jenkins-2053', 'apps',
                    'pull_integration_realcluster_test_next_gen',
                    'presubmit', 'success',
                    0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2053/',
                    'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2053/',
                    'tidb-ci-pull-integration-realcluster-test-next-gen-2053-asdfg', '2026-04-21T12:15:18Z', 'GCP', 'JENKINS'
                  )
                """
            )
        )

    summary = run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        start_time_from=datetime(2026, 4, 21, 0, 0, 0),
        start_time_to=datetime(2026, 4, 22, 0, 0, 0),
        batch_size=1,
    )

    assert summary.reconciled_rows_updated == 2

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT pod_uid, source_prow_job_id, normalized_build_url
                    FROM ci_l1_pod_lifecycle
                    WHERE pod_uid IN ('uid-reconcile-b1', 'uid-reconcile-b2')
                    ORDER BY pod_uid
                    """
                )
            ).mappings()
        )

    assert rows == [
        {
            "pod_uid": "uid-reconcile-b1",
            "source_prow_job_id": "prow-job-jenkins-2052",
            "normalized_build_url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2052/"
            ),
        },
        {
            "pod_uid": "uid-reconcile-b2",
            "source_prow_job_id": "prow-job-jenkins-2053",
            "normalized_build_url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2053/"
            ),
        },
    ]
