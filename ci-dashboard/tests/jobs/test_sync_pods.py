from __future__ import annotations

from datetime import datetime
from urllib import error as urllib_error

import pytest
from sqlalchemy import text

from ci_dashboard.jobs.sync_pods import _load_target_namespaces

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.state_store import get_job_state
from ci_dashboard.jobs.sync_pods import (
    PodMetadataSnapshot,
    _extract_build_number_from_jenkins_label,
    _get_json,
    _post_json,
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
) -> PodMetadataSnapshot:
    return PodMetadataSnapshot(
        pod_uid=pod_uid,
        labels=labels or {},
        annotations=annotations or {},
        observed_at=datetime(2026, 4, 21, 9, 45, 0),
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


def test_sync_pods_end_to_end_and_idempotent(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, url, normalized_build_key,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  1, 'prow-job-1', 'prow-test-pods', 'ghpr_unit_test', 'presubmit', 'failure',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/66206/ghpr_unit_test/1/',
                  '/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/66206/ghpr_unit_test/1',
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
                  jenkins_build_url_key,
                  source_prow_job_id,
                  normalized_build_key,
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
    assert lifecycle["jenkins_build_url_key"] is None
    assert lifecycle["source_prow_job_id"] == "prow-job-1"
    assert lifecycle["normalized_build_key"] == "/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/66206/ghpr_unit_test/1"
    assert lifecycle["repo_full_name"] == "pingcap/tidb"
    assert lifecycle["job_name"] == "ghpr_unit_test"
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
                  optional, report, org, repo, repo_full_name, url, normalized_build_key,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  2, 'prow-job-jenkins-1413', 'apps', 'ghpr_unit_test', 'presubmit', 'failure',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413/',
                  '/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413',
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
                      jenkins_build_url_key,
                      source_prow_job_id,
                      normalized_build_key,
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
    assert {row["jenkins_build_url_key"] for row in rows} == {
        "/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413"
    }
    assert {row["source_prow_job_id"] for row in rows} == {"prow-job-jenkins-1413"}
    assert {row["normalized_build_key"] for row in rows} == {
        "/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1413"
    }
    assert {row["repo_full_name"] for row in rows} == {"pingcap/tidb"}
    assert {row["job_name"] for row in rows} == {"ghpr_unit_test"}
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
                  optional, report, org, repo, repo_full_name, url, normalized_build_key,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  3, 'prow-job-jenkins-299', 'apps',
                  'pingcap/tiflow/pull_dm_integration_test',
                  'presubmit', 'failure',
                  0, 1, 'pingcap', 'tiflow', 'pingcap/tiflow',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/pull_dm_integration_test/299/',
                  '/jenkins/job/pingcap/job/tiflow/job/pull_dm_integration_test/299',
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
                SELECT source_prow_job_id, normalized_build_key, repo_full_name, job_name, pod_annotations_json
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-mid-1'
                """
            )
        ).mappings().one()

    assert row["source_prow_job_id"] == "prow-job-jenkins-299"
    assert row["normalized_build_key"] == (
        "/jenkins/job/pingcap/job/tiflow/job/pull_dm_integration_test/299"
    )
    assert row["repo_full_name"] == "pingcap/tiflow"
    assert row["job_name"] == "pingcap/tiflow/pull_dm_integration_test"
    assert "buildUrl" in row["pod_annotations_json"]


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
                  optional, report, org, repo, repo_full_name, url, normalized_build_key,
                  pod_name, start_time, cloud_phase, build_system
                ) VALUES (
                  4, 'prow-job-jenkins-1029', 'apps',
                  'pingcap/tidb/pull_integration_realcluster_test_next_gen',
                  'presubmit', 'failure',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1029/',
                  '/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1029',
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
                SELECT source_prow_job_id, normalized_build_key, repo_full_name, job_name
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-raw-1'
                """
            )
        ).mappings().one()

    assert row["source_prow_job_id"] == "prow-job-jenkins-1029"
    assert row["normalized_build_key"] == (
        "/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1029"
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
                SELECT build_system, source_prow_job_id, normalized_build_key, pod_labels_json, ci_job
                FROM ci_l1_pod_lifecycle
                WHERE pod_uid = 'uid-amb-1'
                """
            )
        ).mappings().one()

    assert row["build_system"] == "JENKINS"
    assert row["source_prow_job_id"] is None
    assert row["normalized_build_key"] is None
    assert "dm-it-14d6b05a-f21b-498e-a4fc-701d061341a1" in row["pod_labels_json"]
    assert row["ci_job"] == "pingcap/tidb/pull_dm_integration_test"
