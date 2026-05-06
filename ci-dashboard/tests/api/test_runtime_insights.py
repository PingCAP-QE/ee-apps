from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.main import app
from ci_dashboard.api.queries import runtime as runtime_queries


def _insert_build(
    sqlite_engine,
    *,
    build_id: int,
    source_prow_job_id: str,
    job_name: str,
    state: str,
    start_time: str,
    normalized_build_url: str,
    error_l1_category: str | None = None,
    error_l2_subcategory: str | None = None,
    revise_error_l1_category: str | None = None,
    revise_error_l2_subcategory: str | None = None,
    log_gcs_uri: str | None = None,
    build_system: str = "JENKINS",
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  id, source_prow_job_id, namespace, job_name, job_type, state,
                  org, repo, repo_full_name, base_ref, target_branch, url,
                  normalized_build_url, build_id, start_time, completion_time,
                  cloud_phase, build_system, log_gcs_uri,
                  error_l1_category, error_l2_subcategory,
                  revise_error_l1_category, revise_error_l2_subcategory
                ) VALUES (
                  :id, :source_prow_job_id, 'prow', :job_name, 'presubmit', :state,
                  'pingcap', 'tidb', 'pingcap/tidb', 'master', 'master', :url,
                  :normalized_build_url, :build_ref, :start_time, :start_time,
                  'GCP', :build_system, :log_gcs_uri,
                  :error_l1_category, :error_l2_subcategory,
                  :revise_error_l1_category, :revise_error_l2_subcategory
                )
                """
            ),
            {
                "id": build_id,
                "source_prow_job_id": source_prow_job_id,
                "job_name": job_name,
                "state": state,
                "url": f"{normalized_build_url}display/redirect",
                "normalized_build_url": normalized_build_url,
                "build_ref": str(build_id),
                "start_time": start_time,
                "error_l1_category": error_l1_category,
                "error_l2_subcategory": error_l2_subcategory,
                "revise_error_l1_category": revise_error_l1_category,
                "revise_error_l2_subcategory": revise_error_l2_subcategory,
                "log_gcs_uri": log_gcs_uri,
                "build_system": build_system,
            },
        )


def _insert_pod_lifecycle(
    sqlite_engine,
    *,
    source_project: str = "gcp-project",
    namespace_name: str = "ci",
    pod_name: str,
    pod_uid: str,
    normalized_build_url: str,
    source_prow_job_id: str,
    job_name: str,
    pod_created_at: str | None = None,
    first_created_at: str | None = None,
    scheduled_at: str | None,
    first_pulling_at: str | None,
    first_pulled_at: str | None,
    last_failed_scheduling_at: str | None = None,
    failed_scheduling_count: int = 0,
    last_event_at: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_lifecycle (
                  source_project, namespace_name, pod_name, pod_uid, build_system,
                  normalized_build_url, source_prow_job_id, repo_full_name, job_name,
                  pod_created_at,
                  scheduled_at, first_pulling_at, first_pulled_at,
                  first_created_at,
                  last_failed_scheduling_at,
                  failed_scheduling_count, last_event_at
                ) VALUES (
                  :source_project, :namespace_name, :pod_name, :pod_uid, 'JENKINS',
                  :normalized_build_url, :source_prow_job_id, 'pingcap/tidb', :job_name,
                  :pod_created_at,
                  :scheduled_at, :first_pulling_at, :first_pulled_at,
                  :first_created_at,
                  :last_failed_scheduling_at,
                  :failed_scheduling_count, COALESCE(:last_event_at, :first_pulled_at, :first_pulling_at, :scheduled_at, :last_failed_scheduling_at)
                )
                """
            ),
            {
                "source_project": source_project,
                "namespace_name": namespace_name,
                "pod_name": pod_name,
                "pod_uid": pod_uid,
                "normalized_build_url": normalized_build_url,
                "source_prow_job_id": source_prow_job_id,
                "job_name": job_name,
                "pod_created_at": pod_created_at,
                "first_created_at": first_created_at,
                "scheduled_at": scheduled_at,
                "first_pulling_at": first_pulling_at,
                "first_pulled_at": first_pulled_at,
                "last_failed_scheduling_at": last_failed_scheduling_at,
                "failed_scheduling_count": failed_scheduling_count,
                "last_event_at": last_event_at,
            },
        )


def _insert_pod_event(
    sqlite_engine,
    *,
    source_project: str = "gcp-project",
    namespace_name: str = "ci",
    pod_name: str,
    pod_uid: str,
    event_reason: str,
    event_timestamp: str,
    event_message: str = "",
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_events (
                  source_project, namespace_name, pod_name, pod_uid, event_reason,
                  event_type, event_message, event_timestamp, receive_timestamp,
                  source_insert_id
                ) VALUES (
                  :source_project, :namespace_name, :pod_name, :pod_uid, :event_reason,
                  'Warning', :event_message, :event_timestamp, :event_timestamp,
                  :source_insert_id
                )
                """
            ),
            {
                "source_project": source_project,
                "namespace_name": namespace_name,
                "pod_name": pod_name,
                "pod_uid": pod_uid,
                "event_reason": event_reason,
                "event_message": event_message,
                "event_timestamp": event_timestamp,
                "source_insert_id": f"{pod_uid}-{event_reason}-{event_timestamp}",
            },
        )


def test_error_classification_scope_skips_unclassified_no_log_builds(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=401,
        source_prow_job_id="classified-no-log",
        job_name="job-classified",
        state="failure",
        start_time="2026-04-22 10:00:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/classified-no-log/1/",
        error_l1_category="INFRA",
        error_l2_subcategory="K8S",
    )
    _insert_build(
        sqlite_engine,
        build_id=402,
        source_prow_job_id="pending-with-log",
        job_name="job-pending",
        state="failure",
        start_time="2026-04-22 11:00:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/pending-with-log/1/",
        log_gcs_uri="gcs://test-bucket/pending-with-log.log",
    )
    _insert_build(
        sqlite_engine,
        build_id=403,
        source_prow_job_id="prow-native-no-log",
        job_name="job-skipped",
        state="failure",
        start_time="2026-04-22 12:00:00",
        normalized_build_url="https://prow.tidb.net/view/gs/test/prow-native-no-log",
        build_system="PROW_NATIVE",
    )
    _insert_build(
        sqlite_engine,
        build_id=404,
        source_prow_job_id="machine-others-no-log",
        job_name="job-machine-others",
        state="failure",
        start_time="2026-04-22 13:00:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/machine-others-no-log/1/",
        error_l1_category="OTHERS",
        error_l2_subcategory="UNCLASSIFIED",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-insights",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-22",
                    "end_date": "2026-04-22",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["classification_coverage"]["summary"] == {
        "total_failure_like_count": 4,
        "classification_scope_count": 3,
        "skipped_no_log_count": 1,
        "classified_count": 2,
        "unclassified_count": 1,
        "human_revised_count": 0,
        "specific_classified_count": 1,
        "machine_specific_count": 1,
        "machine_others_count": 1,
        "pending_analyze_count": 1,
        "missing_log_count": 1,
        "no_jenkins_log_count": 1,
        "missing_jenkins_log_count": 0,
    }
    l1_items = {item["name"]: item for item in body["error_l1_share"]["items"]}
    assert l1_items["INFRA"]["value"] == 1
    assert l1_items["OTHERS"]["value"] == 2
    assert "job-skipped" not in {
        item["name"]
        for item in body["error_top_jobs"]["items"]
    }


def test_runtime_error_top_jobs_supports_l1_l2_drilldown_and_job_urls(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=451,
        source_prow_job_id="infra-jenkins-a",
        job_name="pingcap/tidb/ghpr_check2",
        state="failure",
        start_time="2026-04-22 09:00:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1/",
        error_l1_category="INFRA",
        error_l2_subcategory="JENKINS",
    )
    _insert_build(
        sqlite_engine,
        build_id=452,
        source_prow_job_id="infra-k8s-a",
        job_name="pingcap/tidb/ghpr_check2",
        state="failure",
        start_time="2026-04-22 09:10:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2/",
        error_l1_category="INFRA",
        error_l2_subcategory="K8S",
    )
    _insert_build(
        sqlite_engine,
        build_id=453,
        source_prow_job_id="build-pipeline-a",
        job_name="pingcap/tidb/ghpr_build",
        state="failure",
        start_time="2026-04-22 09:20:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/1/",
        error_l1_category="BUILD",
        error_l2_subcategory="PIPELINE_CONFIG",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-error-top-jobs",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-22",
                    "end_date": "2026-04-22",
                    "error_l1_category": "INFRA",
                    "error_l2_subcategory": "JENKINS",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [
        {
            "name": "pingcap/tidb/ghpr_check2",
            "value": 1,
            "infra_count": 1,
            "job_url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/",
        }
    ]


def test_runtime_error_builds_returns_build_links_sorted_by_completion_time(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=461,
        source_prow_job_id="infra-jenkins-b1",
        job_name="pingcap/tidb/ghpr_check2",
        state="failure",
        start_time="2026-04-22 08:00:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1001/",
        error_l1_category="INFRA",
        error_l2_subcategory="JENKINS",
    )
    _insert_build(
        sqlite_engine,
        build_id=462,
        source_prow_job_id="infra-jenkins-b2",
        job_name="pingcap/tidb/ghpr_check2",
        state="failure",
        start_time="2026-04-22 09:00:00",
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1002/",
        error_l1_category="INFRA",
        error_l2_subcategory="JENKINS",
    )

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ci_l1_builds
                SET completion_time = :completion_time
                WHERE id = :id
                """
            ),
            {"id": 461, "completion_time": "2026-04-22 10:00:00"},
        )
        connection.execute(
            text(
                """
                UPDATE ci_l1_builds
                SET completion_time = :completion_time
                WHERE id = :id
                """
            ),
            {"id": 462, "completion_time": "2026-04-22 11:00:00"},
        )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-error-builds",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-22",
                    "end_date": "2026-04-22",
                    "selected_job_name": "pingcap/tidb/ghpr_check2",
                    "error_l1_category": "INFRA",
                    "error_l2_subcategory": "JENKINS",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [
        {
            "name": "1002",
            "build_number": "1002",
            "build_url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1002/",
            "completion_time": "2026-04-22T11:00:00Z",
        },
        {
            "name": "1001",
            "build_number": "1001",
            "build_url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1001/",
            "completion_time": "2026-04-22T10:00:00Z",
        },
    ]


def test_runtime_error_builds_defaults_to_latest_15_records(sqlite_engine) -> None:
    for index in range(17):
        build_id = 600 + index
        build_number = 2000 + index
        start_hour = 1 + index
        _insert_build(
            sqlite_engine,
            build_id=build_id,
            source_prow_job_id=f"infra-jenkins-limit-{index}",
            job_name="pingcap/tidb/ghpr_check2",
            state="failure",
            start_time=f"2026-04-22 {start_hour:02d}:00:00",
            normalized_build_url=(
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/"
                f"{build_number}/"
            ),
            error_l1_category="INFRA",
            error_l2_subcategory="JENKINS",
        )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-error-builds",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-22",
                    "end_date": "2026-04-22",
                    "selected_job_name": "pingcap/tidb/ghpr_check2",
                    "error_l1_category": "INFRA",
                    "error_l2_subcategory": "JENKINS",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 15
    assert body["items"][0]["build_number"] == "2016"
    assert body["items"][-1]["build_number"] == "2002"


def test_runtime_insights_rolls_pods_to_builds_and_uses_effective_categories(sqlite_engine) -> None:
    build_a_url = "https://prow.tidb.net/jenkins/job/runtime-a/1/"
    build_b_url = "https://prow.tidb.net/jenkins/job/runtime-b/1/"
    build_c_url = "https://prow.tidb.net/jenkins/job/runtime-c/1/"
    _insert_build(
        sqlite_engine,
        build_id=101,
        source_prow_job_id="runtime-a",
        job_name="job-alpha",
        state="failure",
        start_time="2026-04-20 10:00:00",
        normalized_build_url=build_a_url,
        error_l1_category="BUILD",
        error_l2_subcategory="COMPILE",
        revise_error_l1_category="INFRA",
        revise_error_l2_subcategory="K8S",
    )
    _insert_build(
        sqlite_engine,
        build_id=102,
        source_prow_job_id="runtime-b",
        job_name="job-beta",
        state="failure",
        start_time="2026-04-20 11:00:00",
        normalized_build_url=build_b_url,
        log_gcs_uri="gcs://test-bucket/runtime-b.log",
    )
    _insert_build(
        sqlite_engine,
        build_id=103,
        source_prow_job_id="runtime-c",
        job_name="job-alpha",
        state="success",
        start_time="2026-04-20 12:00:00",
        normalized_build_url=build_c_url,
    )

    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-a-fast",
        pod_uid="pod-a-fast-uid",
        normalized_build_url=build_a_url,
        source_prow_job_id="runtime-a",
        job_name="job-alpha",
        pod_created_at="2026-04-20 09:59:00",
        scheduled_at="2026-04-20 10:00:00",
        first_pulling_at="2026-04-20 10:01:00",
        first_pulled_at="2026-04-20 10:01:20",
        last_failed_scheduling_at="2026-04-20 09:59:00",
        failed_scheduling_count=1,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-a-slow",
        pod_uid="pod-a-slow-uid",
        normalized_build_url=build_a_url,
        source_prow_job_id="runtime-a",
        job_name="job-alpha",
        pod_created_at="2026-04-20 09:57:00",
        scheduled_at="2026-04-20 10:00:00",
        first_pulling_at="2026-04-20 10:02:00",
        first_pulled_at="2026-04-20 10:02:40",
        last_failed_scheduling_at="2026-04-20 09:58:00",
        failed_scheduling_count=1,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-a-fail",
        pod_uid="pod-a-fail-uid",
        normalized_build_url=build_a_url,
        source_prow_job_id="runtime-a",
        job_name="job-alpha",
        scheduled_at="2026-04-20 10:00:00",
        first_pulling_at="2026-04-20 10:03:00",
        first_pulled_at=None,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-c",
        pod_uid="pod-c-uid",
        normalized_build_url=build_c_url,
        source_prow_job_id="runtime-c",
        job_name="job-alpha",
        pod_created_at="2026-04-20 11:59:00",
        scheduled_at="2026-04-20 12:00:00",
        first_pulling_at="2026-04-20 12:01:00",
        first_pulled_at="2026-04-20 12:01:10",
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-b-unscheduled",
        pod_uid="pod-b-unscheduled-uid",
        normalized_build_url=build_b_url,
        source_prow_job_id="runtime-b",
        job_name="job-beta",
        scheduled_at=None,
        first_pulling_at=None,
        first_pulled_at=None,
        last_failed_scheduling_at="2026-04-20 11:05:00",
        failed_scheduling_count=2,
    )

    _insert_pod_event(
        sqlite_engine,
        pod_name="pod-a-fast",
        pod_uid="pod-a-fast-uid",
        event_reason="FailedScheduling",
        event_timestamp="2026-04-20 09:59:00",
    )
    _insert_pod_event(
        sqlite_engine,
        pod_name="pod-a-slow",
        pod_uid="pod-a-slow-uid",
        event_reason="FailedScheduling",
        event_timestamp="2026-04-20 09:58:00",
    )
    _insert_pod_event(
        sqlite_engine,
        pod_name="pod-a-fail",
        pod_uid="pod-a-fail-uid",
        event_reason="ErrImagePull",
        event_timestamp="2026-04-20 10:02:05",
        event_message="failed to pull image",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-insights",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-20",
                    "end_date": "2026-04-20",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()

    summary = body["runtime_summary"]
    assert summary["total_build_count"] == 3
    assert summary["builds_with_pod_count"] == 3
    assert summary["linked_pod_row_count"] == 5
    assert summary["linked_build_count"] == 3
    assert summary["scheduling_wait_supported"] is True
    assert summary["avg_scheduling_wait_s"] == 120
    assert summary["final_scheduling_failure_count"] == 1
    assert summary["avg_pull_image_s"] == 25
    assert summary["pull_image_success_rate_pct"] == 50.0

    scheduling_series = {
        item["key"]: item
        for item in body["scheduling_trend"]["series"]
    }
    assert scheduling_series["scheduling_wait_avg_s"]["points"] == [["2026-04-20", 120.0]]
    assert scheduling_series["final_scheduling_failure_count"]["points"] == [["2026-04-20", 1]]
    assert body["scheduling_trend"]["meta"]["sample_counts"] == [
        {
            "bucket_start": "2026-04-20",
            "linked_build_count": 3,
            "valid_sample_count": 2,
        }
    ]

    scheduling_failures = body["scheduling_failure_jobs"]["items"]
    assert scheduling_failures == []

    pull_failures = body["pull_image_failure_jobs"]["items"]
    assert pull_failures[0]["name"] == "pingcap/tidb/job-alpha"
    assert pull_failures[0]["value"] == 1
    assert pull_failures[0]["linked_build_count"] == 2
    assert pull_failures[0]["job_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-alpha/"
    assert "pull_image_failure_reasons" not in body

    l1_items = {item["name"]: item for item in body["error_l1_share"]["items"]}
    assert l1_items["INFRA"]["value"] == 1
    assert l1_items["OTHERS"]["value"] == 1
    assert body["error_l1_share"]["l2_details"]["INFRA"] == [
        {"name": "K8S", "value": 1, "share_pct": 100.0}
    ]
    assert body["error_l1_share"]["l2_details"]["OTHERS"] == [
        {"name": "UNCLASSIFIED", "value": 1, "share_pct": 100.0}
    ]
    assert body["error_l2_trends"]["items"]["INFRA"]["series"] == [
        {
            "key": "K8S",
            "label": "K8S",
            "type": "bar",
            "points": [["2026-04-20", 1]],
        }
    ]
    assert body["classification_coverage"]["summary"] == {
        "total_failure_like_count": 2,
        "classification_scope_count": 2,
        "skipped_no_log_count": 0,
        "classified_count": 1,
        "unclassified_count": 1,
        "human_revised_count": 1,
        "specific_classified_count": 1,
        "machine_specific_count": 0,
        "machine_others_count": 0,
        "pending_analyze_count": 1,
        "missing_log_count": 0,
        "no_jenkins_log_count": 0,
        "missing_jenkins_log_count": 0,
    }


def test_scheduling_failure_jobs_include_latest_ten_failure_build_links(sqlite_engine) -> None:
    for index in range(12):
        build_number = 3000 + index
        start_hour = 1 + index
        build_url = f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-gamma/{build_number}/"
        _insert_build(
            sqlite_engine,
            build_id=800 + index,
            source_prow_job_id=f"runtime-gamma-{index}",
            job_name="job-gamma",
            state="failure",
            start_time=f"2026-04-23 {start_hour:02d}:00:00",
            normalized_build_url=build_url,
        )
        _insert_pod_lifecycle(
            sqlite_engine,
            pod_name=f"pod-gamma-{index}",
            pod_uid=f"pod-gamma-{index}-uid",
            normalized_build_url=build_url,
            source_prow_job_id=f"runtime-gamma-{index}",
            job_name="job-gamma",
            scheduled_at=None,
            first_pulling_at=None,
            first_pulled_at=None,
            last_failed_scheduling_at=f"2026-04-23 {start_hour:02d}:05:00",
            failed_scheduling_count=1,
            last_event_at=f"2026-04-23 {start_hour:02d}:05:00",
        )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-insights",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-23",
                    "end_date": "2026-04-23",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    scheduling_failures = body["scheduling_failure_jobs"]["items"]
    assert scheduling_failures[0]["name"] == "pingcap/tidb/job-gamma"
    assert scheduling_failures[0]["final_failure_count"] == 12
    assert [item["build_number"] for item in scheduling_failures[0]["recent_failure_builds"]] == [
        "3011",
        "3010",
        "3009",
        "3008",
        "3007",
        "3006",
        "3005",
        "3004",
        "3003",
        "3002",
    ]
    assert (
        scheduling_failures[0]["recent_failure_builds"][0]["build_url"]
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-gamma/3011/"
    )


def test_runtime_trends_use_pod_time_buckets_and_true_scheduling_wait(sqlite_engine) -> None:
    build_url = "https://prow.tidb.net/jenkins/job/runtime-shifted/1/"
    _insert_build(
        sqlite_engine,
        build_id=201,
        source_prow_job_id="runtime-shifted",
        job_name="job-shifted",
        state="failure",
        start_time="2026-04-02 08:00:00",
        normalized_build_url=build_url,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-shifted",
        pod_uid="pod-shifted-uid",
        normalized_build_url=build_url,
        source_prow_job_id="runtime-shifted",
        job_name="job-shifted",
        pod_created_at="2026-04-21 08:45:00",
        scheduled_at="2026-04-21 09:00:00",
        first_pulling_at="2026-04-21 09:02:00",
        first_pulled_at="2026-04-21 09:02:20",
        last_failed_scheduling_at="2026-04-21 08:59:00",
        failed_scheduling_count=1,
    )
    _insert_pod_event(
        sqlite_engine,
        pod_name="pod-shifted",
        pod_uid="pod-shifted-uid",
        event_reason="FailedScheduling",
        event_timestamp="2026-04-21 08:50:00",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-insights",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-03-30",
                    "end_date": "2026-04-30",
                    "granularity": "week",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()

    scheduling_points = {
        item["key"]: item["points"]
        for item in body["scheduling_trend"]["series"]
    }
    pull_points = {
        item["key"]: item["points"]
        for item in body["pull_image_trend"]["series"]
    }
    assert scheduling_points["scheduling_wait_avg_s"] == [["2026-04-20", 900.0]]
    assert scheduling_points["final_scheduling_failure_count"] == [["2026-04-20", 0]]
    assert pull_points["pull_image_avg_s"] == [["2026-04-20", 20.0]]
    assert pull_points["pull_image_success_rate_pct"] == [["2026-04-20", 100.0]]


def test_pull_image_slowest_jobs_include_slowest_image_url(sqlite_engine) -> None:
    build_url_1 = "https://prow.tidb.net/jenkins/job/runtime-pull-image/1/"
    build_url_2 = "https://prow.tidb.net/jenkins/job/runtime-pull-image/2/"
    build_url_3 = "https://prow.tidb.net/jenkins/job/runtime-pull-image/3/"

    _insert_build(
        sqlite_engine,
        build_id=901,
        source_prow_job_id="runtime-pull-image-1",
        job_name="job-pull-image",
        state="failure",
        start_time="2026-04-24 10:00:00",
        normalized_build_url=build_url_1,
    )
    _insert_build(
        sqlite_engine,
        build_id=902,
        source_prow_job_id="runtime-pull-image-2",
        job_name="job-pull-image",
        state="failure",
        start_time="2026-04-24 11:00:00",
        normalized_build_url=build_url_2,
    )
    _insert_build(
        sqlite_engine,
        build_id=903,
        source_prow_job_id="runtime-pull-image-3",
        job_name="job-pull-image",
        state="failure",
        start_time="2026-04-24 12:00:00",
        normalized_build_url=build_url_3,
    )

    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-pull-image-fast",
        pod_uid="pod-pull-image-fast-uid",
        normalized_build_url=build_url_1,
        source_prow_job_id="runtime-pull-image-1",
        job_name="job-pull-image",
        scheduled_at="2026-04-24 10:00:40",
        first_pulling_at="2026-04-24 10:01:00",
        first_pulled_at="2026-04-24 10:01:20",
        failed_scheduling_count=0,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-pull-image-slow",
        pod_uid="pod-pull-image-slow-uid",
        normalized_build_url=build_url_2,
        source_prow_job_id="runtime-pull-image-2",
        job_name="job-pull-image",
        scheduled_at="2026-04-24 11:00:30",
        first_pulling_at="2026-04-24 11:01:00",
        first_pulled_at="2026-04-24 11:02:30",
        failed_scheduling_count=0,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-pull-image-mid",
        pod_uid="pod-pull-image-mid-uid",
        normalized_build_url=build_url_2,
        source_prow_job_id="runtime-pull-image-2",
        job_name="job-pull-image",
        scheduled_at="2026-04-24 11:02:40",
        first_pulling_at="2026-04-24 11:03:00",
        first_pulled_at="2026-04-24 11:03:40",
        failed_scheduling_count=0,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-pull-image-third",
        pod_uid="pod-pull-image-third-uid",
        normalized_build_url=build_url_3,
        source_prow_job_id="runtime-pull-image-3",
        job_name="job-pull-image",
        scheduled_at="2026-04-24 12:00:20",
        first_pulling_at="2026-04-24 12:01:00",
        first_pulled_at="2026-04-24 12:01:30",
        failed_scheduling_count=0,
    )

    _insert_pod_event(
        sqlite_engine,
        pod_name="pod-pull-image-fast",
        pod_uid="pod-pull-image-fast-uid",
        event_reason="Pulled",
        event_timestamp="2026-04-24 10:01:20",
        event_message='Successfully pulled image "registry.example.com/fast:v1" in 20.5s (20.5s including waiting). Image size: 123 bytes.',
    )
    _insert_pod_event(
        sqlite_engine,
        pod_name="pod-pull-image-slow",
        pod_uid="pod-pull-image-slow-uid",
        event_reason="Pulled",
        event_timestamp="2026-04-24 11:02:30",
        event_message='Successfully pulled image "registry.example.com/slow:v9" in 90.1s (90.1s including waiting). Image size: 456 bytes.',
    )
    _insert_pod_event(
        sqlite_engine,
        pod_name="pod-pull-image-third",
        pod_uid="pod-pull-image-third-uid",
        event_reason="Pulled",
        event_timestamp="2026-04-24 12:01:30",
        event_message='Successfully pulled image "registry.example.com/third:v3" in 30.0s (30.0s including waiting). Image size: 789 bytes.',
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-insights",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-24",
                    "end_date": "2026-04-24",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    slowest_jobs = body["pull_image_slowest_jobs"]["items"]
    assert slowest_jobs[0]["name"] == "pingcap/tidb/job-pull-image"
    assert slowest_jobs[0]["slowest_pull_image"] == "registry.example.com/slow:v9"


def test_runtime_hides_scheduling_wait_when_pod_created_at_is_unavailable(
    sqlite_engine,
    monkeypatch,
) -> None:
    build_url = "https://prow.tidb.net/jenkins/job/runtime-no-created-at/1/"
    _insert_build(
        sqlite_engine,
        build_id=301,
        source_prow_job_id="runtime-no-created-at",
        job_name="job-no-created-at",
        state="failure",
        start_time="2026-04-21 08:00:00",
        normalized_build_url=build_url,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        pod_name="pod-no-created-at",
        pod_uid="pod-no-created-at-uid",
        normalized_build_url=build_url,
        source_prow_job_id="runtime-no-created-at",
        job_name="job-no-created-at",
        pod_created_at="2026-04-21 08:45:00",
        scheduled_at="2026-04-21 09:00:00",
        first_pulling_at="2026-04-21 09:02:00",
        first_pulled_at="2026-04-21 09:02:20",
        last_failed_scheduling_at="2026-04-21 08:59:00",
        failed_scheduling_count=1,
    )

    monkeypatch.setattr(runtime_queries, "_table_has_column", lambda *_args, **_kwargs: False)
    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/pages/runtime-insights",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-21",
                    "end_date": "2026-04-21",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_summary"]["scheduling_wait_supported"] is False
    assert body["runtime_summary"]["avg_scheduling_wait_s"] is None
    assert body["runtime_summary"]["valid_scheduling_sample_count"] == 0
    assert body["scheduling_trend"]["series"] == [
        {
            "key": "final_scheduling_failure_count",
            "label": "Final scheduling failures",
            "type": "line",
            "axis": "right",
            "points": [["2026-04-21", 0]],
        }
    ]
    assert body["scheduling_slowest_jobs"]["items"] == []
