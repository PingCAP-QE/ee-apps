from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.main import app, create_app


def _insert_build(
    sqlite_engine,
    *,
    source_prow_row_id: int,
    source_prow_job_id: str,
    repo_full_name: str,
    target_branch: str | None,
    base_ref: str | None,
    job_name: str,
    state: str,
    cloud_phase: str,
    is_flaky: int,
    is_retry_loop: int,
    failure_category: str | None,
    start_time: str,
    queue_wait_seconds: int = 0,
    run_seconds: int = 0,
    total_seconds: int = 0,
    pr_number: int = 100,
    normalized_build_key: str | None = None,
    build_id: str = "1",
) -> None:
    org, repo = repo_full_name.split("/", 1)
    build_key = normalized_build_key or f"/jenkins/job/{source_prow_job_id}"
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, base_ref, pr_number, is_pr_build,
                  context, url, normalized_build_key, author, retest, event_guid, build_id,
                  pod_name, pending_time, start_time, completion_time, queue_wait_seconds,
                  run_seconds, total_seconds, head_sha, target_branch, cloud_phase, is_flaky,
                  is_retry_loop, has_flaky_case_match, failure_category, failure_subcategory
                ) VALUES (
                  :source_prow_row_id, :source_prow_job_id, 'prow', :job_name, 'presubmit', :state,
                  0, 1, :org, :repo, :repo_full_name, :base_ref, :pr_number, 1,
                  'unit-test', :url,
                  :normalized_build_key, 'alice', 0, 'guid', :build_id, NULL, NULL, :start_time,
                  :start_time, :queue_wait_seconds, :run_seconds, :total_seconds, 'sha', :target_branch, :cloud_phase, :is_flaky,
                  :is_retry_loop, 0, :failure_category, NULL
                )
                """
            ),
            {
                "source_prow_row_id": source_prow_row_id,
                "source_prow_job_id": source_prow_job_id,
                "job_name": job_name,
                "state": state,
                "org": org,
                "repo": repo,
                "repo_full_name": repo_full_name,
                "base_ref": base_ref,
                "pr_number": pr_number,
                "url": f"https://prow.tidb.net{build_key}/display/redirect",
                "normalized_build_key": build_key,
                "build_id": build_id,
                "start_time": start_time,
                "queue_wait_seconds": queue_wait_seconds,
                "run_seconds": run_seconds,
                "total_seconds": total_seconds,
                "target_branch": target_branch,
                "cloud_phase": cloud_phase,
                "is_flaky": is_flaky,
                "is_retry_loop": is_retry_loop,
                "failure_category": failure_category,
            },
        )


def _insert_success_run_series(
    sqlite_engine,
    *,
    start_source_prow_row_id: int,
    source_prefix: str,
    repo_full_name: str,
    target_branch: str,
    job_name: str,
    cloud_phase: str,
    normalized_job_path: str,
    start_times: list[str],
    run_seconds_list: list[int],
) -> None:
    for index, (start_time, run_seconds) in enumerate(zip(start_times, run_seconds_list), start=0):
        _insert_build(
            sqlite_engine,
            source_prow_row_id=start_source_prow_row_id + index,
            source_prow_job_id=f"{source_prefix}-{index + 1}",
            repo_full_name=repo_full_name,
            target_branch=target_branch,
            base_ref=target_branch,
            job_name=job_name,
            state="success",
            cloud_phase=cloud_phase,
            is_flaky=0,
            is_retry_loop=0,
            failure_category=None,
            start_time=start_time,
            queue_wait_seconds=max(run_seconds // 10, 1),
            run_seconds=run_seconds,
            total_seconds=run_seconds + max(run_seconds // 10, 1),
            pr_number=300 + start_source_prow_row_id + index,
            normalized_build_key=f"{normalized_job_path}/{start_source_prow_row_id + index}",
            build_id=f"prow-{start_source_prow_row_id + index}",
        )


def _insert_pr_event(
    sqlite_engine,
    *,
    repo: str,
    pr_number: int,
    target_branch: str,
    event_key: str,
    event_time: str,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pr_events (
                  repo, pr_number, event_key, event_time, event_type, actor_login, comment_id,
                  comment_body, retest_event, commit_sha, target_branch, head_ref, head_sha,
                  created_at, updated_at
                ) VALUES (
                  :repo, :pr_number, :event_key, :event_time, 'pr_snapshot', NULL, NULL,
                  NULL, 0, NULL, :target_branch, 'feature-x', 'sha', :event_time, :event_time
                )
                """
            ),
            {
                "repo": repo,
                "pr_number": pr_number,
                "event_key": event_key,
                "event_time": event_time,
                "target_branch": target_branch,
            },
        )


def _insert_problem_case_run(
    sqlite_engine,
    *,
    repo: str,
    branch: str,
    case_name: str,
    build_url: str,
    flaky: int,
    report_time: str,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO problem_case_runs (
                  repo, branch, suite_name, case_name, flaky, timecost_ms, report_time, build_url, reason
                ) VALUES (
                  :repo, :branch, 'unit', :case_name, :flaky, 10, :report_time, :build_url, 'flake'
                )
                """
            ),
            {
                "repo": repo,
                "branch": branch,
                "case_name": case_name,
                "build_url": build_url,
                "flaky": flaky,
                "report_time": report_time,
            },
        )


def _insert_flaky_issue(
    sqlite_engine,
    *,
    repo: str,
    issue_number: int,
    case_name: str,
    issue_branch: str,
    issue_status: str,
    issue_created_at: str,
    issue_closed_at: str | None = None,
    last_reopened_at: str | None = None,
    reopen_count: int = 0,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_flaky_issues (
                  repo, issue_number, issue_url, issue_title, case_name, issue_status, issue_branch,
                  branch_source, issue_created_at, issue_updated_at, issue_closed_at, last_reopened_at,
                  reopen_count, source_ticket_id, source_ticket_updated_at
                ) VALUES (
                  :repo, :issue_number, :issue_url, :issue_title, :case_name, :issue_status, :issue_branch,
                  'issue_html', :issue_created_at, :issue_updated_at, :issue_closed_at, :last_reopened_at,
                  :reopen_count, :source_ticket_id, :source_ticket_updated_at
                )
                """
            ),
            {
                "repo": repo,
                "issue_number": issue_number,
                "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
                "issue_title": f"Flaky test: {case_name} in nightly",
                "case_name": case_name,
                "issue_status": issue_status,
                "issue_branch": issue_branch,
                "issue_created_at": issue_created_at,
                "issue_updated_at": issue_created_at,
                "issue_closed_at": issue_closed_at,
                "last_reopened_at": last_reopened_at,
                "reopen_count": reopen_count,
                "source_ticket_id": issue_number,
                "source_ticket_updated_at": issue_created_at,
            },
        )


def _insert_job_state(
    sqlite_engine,
    *,
    job_name: str,
    last_status: str,
    last_succeeded_at: str | None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_job_state (
                  job_name, watermark_json, last_started_at, last_succeeded_at, last_status, last_error, updated_at
                ) VALUES (
                  :job_name, '{}', :last_succeeded_at, :last_succeeded_at, :last_status, NULL, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "job_name": job_name,
                "last_status": last_status,
                "last_succeeded_at": last_succeeded_at,
            },
        )


@pytest.fixture()
def api_client(sqlite_engine, monkeypatch):
    _insert_build(
        sqlite_engine,
        source_prow_row_id=1,
        source_prow_job_id="job-1",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-a",
        state="failure",
        cloud_phase="GCP",
        is_flaky=1,
        is_retry_loop=0,
        failure_category="FLAKY_TEST",
        start_time="2026-04-10 10:00:00",
        queue_wait_seconds=30,
        run_seconds=300,
        total_seconds=330,
        pr_number=100,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=2,
        source_prow_job_id="job-2",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-a",
        state="failure",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=1,
        failure_category="FLAKY_TEST",
        start_time="2026-04-10 10:05:00",
        queue_wait_seconds=45,
        run_seconds=180,
        total_seconds=225,
        pr_number=100,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=3,
        source_prow_job_id="job-3",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-b",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 10:10:00",
        queue_wait_seconds=120,
        run_seconds=600,
        total_seconds=720,
        pr_number=101,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=4,
        source_prow_job_id="job-4",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-b",
        state="failure",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-11 10:00:00",
        queue_wait_seconds=15,
        run_seconds=90,
        total_seconds=105,
        pr_number=102,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=5,
        source_prow_job_id="job-5",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-c",
        state="timeout",
        cloud_phase="GCP",
        is_flaky=1,
        is_retry_loop=0,
        failure_category="FLAKY_TEST",
        start_time="2026-04-11 10:10:00",
        queue_wait_seconds=20,
        run_seconds=150,
        total_seconds=170,
        pr_number=102,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=6,
        source_prow_job_id="job-6",
        repo_full_name="pingcap/tiflash",
        target_branch="release-8.5",
        base_ref="release-8.5",
        job_name="job-d",
        state="failure",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-11 11:00:00",
        queue_wait_seconds=25,
        run_seconds=200,
        total_seconds=225,
        pr_number=200,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=7,
        source_prow_job_id="job-7",
        repo_full_name="pingcap/tidb",
        target_branch="main",
        base_ref="main",
        job_name="job-e",
        state="success",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-11 11:30:00",
        queue_wait_seconds=90,
        run_seconds=450,
        total_seconds=540,
        pr_number=103,
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=100,
        source_prefix="job-fast-idc",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-fast",
        cloud_phase="IDC",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-fast",
        start_times=[
            "2026-02-20 09:00:00",
            "2026-02-21 09:00:00",
            "2026-02-22 09:00:00",
            "2026-02-23 09:00:00",
            "2026-02-24 09:00:00",
        ],
        run_seconds_list=[600, 620, 580, 610, 590],
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=110,
        source_prefix="job-fast-gcp",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-fast",
        cloud_phase="GCP",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-fast",
        start_times=[
            "2026-03-01 09:00:00",
            "2026-04-02 09:00:00",
            "2026-04-03 09:00:00",
            "2026-04-04 09:00:00",
            "2026-04-05 09:00:00",
            "2026-04-06 09:00:00",
        ],
        run_seconds_list=[500, 300, 320, 280, 290, 310],
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=120,
        source_prefix="job-slow-idc",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-slow",
        cloud_phase="IDC",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-slow",
        start_times=[
            "2026-02-20 10:00:00",
            "2026-02-21 10:00:00",
            "2026-02-22 10:00:00",
            "2026-02-23 10:00:00",
            "2026-02-24 10:00:00",
        ],
        run_seconds_list=[180, 200, 220, 210, 190],
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=130,
        source_prefix="job-slow-gcp",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-slow",
        cloud_phase="GCP",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-slow",
        start_times=[
            "2026-03-01 10:00:00",
            "2026-04-02 10:00:00",
            "2026-04-03 10:00:00",
            "2026-04-04 10:00:00",
            "2026-04-05 10:00:00",
            "2026-04-06 10:00:00",
        ],
        run_seconds_list=[210, 480, 500, 520, 510, 490],
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=100,
        target_branch="master",
        event_key="pr-100-master",
        event_time="2026-04-10 09:30:00",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=101,
        target_branch="master",
        event_key="pr-101-master",
        event_time="2026-04-10 09:35:00",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=102,
        target_branch="master",
        event_key="pr-102-master",
        event_time="2026-04-11 09:35:00",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tiflash",
        pr_number=200,
        target_branch="release-8.5",
        event_key="pr-200-release85",
        event_time="2026-04-11 10:30:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestCaseAlpha",
        build_url="https://prow.tidb.net/jenkins/job/job-1/display/redirect",
        flaky=1,
        report_time="2026-04-10 10:20:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestCaseBeta",
        build_url="https://prow.tidb.net/jenkins/job/job-5/display/redirect",
        flaky=1,
        report_time="2026-04-11 10:20:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=66726,
        case_name="TestCaseAlpha",
        issue_branch="master",
        issue_status="open",
        issue_created_at="2026-04-09 08:00:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=66982,
        case_name="TestCaseBeta",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:10:00",
        issue_closed_at="2026-04-12 09:00:00",
    )
    _insert_job_state(
        sqlite_engine,
        job_name="ci-sync-builds",
        last_status="succeeded",
        last_succeeded_at="2026-04-12T09:45:00Z",
    )
    _insert_job_state(
        sqlite_engine,
        job_name="ci-refresh-build-derived",
        last_status="running",
        last_succeeded_at="2026-04-12T09:30:00Z",
    )

    monkeypatch.setattr(
        "ci_dashboard.api.queries.status.utcnow",
        lambda: datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    with TestClient(app) as client:
        yield client


def test_frontend_serves_static_file_within_dist(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    app_root = repo_root / "ci-dashboard"
    dist_dir = app_root / "web" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist_dir / "dashboard.txt").write_text("dashboard-ok", encoding="utf-8")

    fake_main = app_root / "src" / "ci_dashboard" / "api" / "main.py"
    fake_main.parent.mkdir(parents=True)
    fake_main.write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr("ci_dashboard.api.main.__file__", str(fake_main))

    test_app = create_app()
    with TestClient(test_app) as client:
        response = client.get("/dashboard.txt")

    assert response.status_code == 200
    assert response.text == "dashboard-ok"


def test_frontend_blocks_path_traversal_outside_dist(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    app_root = repo_root / "ci-dashboard"
    dist_dir = app_root / "web" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>spa-shell</html>", encoding="utf-8")
    (app_root / "secret.txt").write_text("top-secret", encoding="utf-8")

    fake_main = app_root / "src" / "ci_dashboard" / "api" / "main.py"
    fake_main.parent.mkdir(parents=True)
    fake_main.write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr("ci_dashboard.api.main.__file__", str(fake_main))

    test_app = create_app()
    with TestClient(test_app) as client:
        response = client.get("/..%2Fsecret.txt")

    assert response.status_code == 200
    assert response.text == "<html>spa-shell</html>"
    assert response.text != "top-secret"
    app.dependency_overrides.clear()


def test_frontend_uses_configured_static_dir(tmp_path: Path, monkeypatch) -> None:
    dist_dir = tmp_path / "custom-dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>configured-spa</html>", encoding="utf-8")
    (dist_dir / "configured.txt").write_text("configured-ok", encoding="utf-8")

    monkeypatch.setenv("CI_DASHBOARD_STATIC_DIR", str(dist_dir))

    test_app = create_app()
    with TestClient(test_app) as client:
        response = client.get("/configured.txt")

    assert response.status_code == 200
    assert response.text == "configured-ok"


def test_status_and_filter_endpoints(api_client: TestClient) -> None:
    freshness = api_client.get("/api/v1/status/freshness")
    assert freshness.status_code == 200
    freshness_body = freshness.json()
    assert freshness_body["generated_at"] == "2026-04-12T10:00:00Z"
    assert [job["job_name"] for job in freshness_body["jobs"]] == [
        "ci-refresh-build-derived",
        "ci-sync-builds",
    ]
    assert freshness_body["jobs"][0]["lag_minutes"] == 30
    assert freshness_body["jobs"][1]["lag_minutes"] == 15

    repos = api_client.get("/api/v1/filters/repos")
    assert repos.status_code == 200
    assert repos.json()["items"] == [
        {"value": "pingcap/tidb", "label": "pingcap/tidb"},
        {"value": "pingcap/tiflash", "label": "pingcap/tiflash"},
    ]

    branches = api_client.get("/api/v1/filters/branches", params={"repo": "pingcap/tidb"})
    assert branches.status_code == 200
    assert branches.json()["items"] == [
        {"value": "main", "label": "main"},
        {"value": "master", "label": "master"},
    ]

    jobs = api_client.get(
        "/api/v1/filters/jobs",
        params={"repo": "pingcap/tidb", "branch": "master"},
    )
    assert jobs.status_code == 200
    assert jobs.json()["items"] == [
        {"value": "job-a", "label": "job-a"},
        {"value": "job-b", "label": "job-b"},
        {"value": "job-c", "label": "job-c"},
        {"value": "job-fast", "label": "job-fast"},
        {"value": "job-slow", "label": "job-slow"},
    ]

    cloud_phases = api_client.get("/api/v1/filters/cloud-phases")
    assert cloud_phases.status_code == 200
    assert cloud_phases.json()["items"] == [
        {"value": "GCP", "label": "GCP"},
        {"value": "IDC", "label": "IDC"},
    ]


def test_flaky_trend_and_composition(api_client: TestClient) -> None:
    trend = api_client.get(
        "/api/v1/flaky/trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert trend.status_code == 200
    trend_body = trend.json()
    series = {item["key"]: item for item in trend_body["series"]}
    assert series["flaky_rate_pct"]["points"] == [["2026-04-10", 50.0], ["2026-04-11", 50.0]]
    assert series["flaky_rate_pct"]["axis"] == "right"
    assert series["total_failure_like_count"]["points"] == [["2026-04-10", 2], ["2026-04-11", 2]]
    assert series["total_failure_like_count"]["axis"] == "left"
    assert trend_body["meta"]["repo"] == "pingcap/tidb"
    assert trend_body["meta"]["branch"] == "master"

    composition = api_client.get(
        "/api/v1/flaky/composition",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert composition.status_code == 200
    composition_body = composition.json()
    composition_series = {item["key"]: item for item in composition_body["series"]}
    assert composition_series["flaky_rate_pct"]["points"] == [["2026-04-10", 50.0], ["2026-04-11", 50.0]]
    assert composition_series["flaky_rate_pct"]["axis"] == "right"
    assert composition_series["retry_loop_rate_pct"]["points"] == [["2026-04-10", 50.0], ["2026-04-11", 0.0]]
    assert composition_series["retry_loop_rate_pct"]["axis"] == "right"
    assert composition_series["noisy_rate_pct"]["points"] == [["2026-04-10", 100.0], ["2026-04-11", 50.0]]
    assert composition_series["noisy_rate_pct"]["axis"] == "right"
    assert composition_series["total_failure_like_count"]["axis"] == "left"

    distinct_counts = api_client.get(
        "/api/v1/flaky/distinct-case-counts",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert distinct_counts.status_code == 200
    distinct_counts_body = distinct_counts.json()
    assert distinct_counts_body["weeks"] == ["2026-04-06"]
    assert distinct_counts_body["rows"] == [{"branch": "master", "values": [2]}]

    issue_weekly_rates = api_client.get(
        "/api/v1/flaky/issue-weekly-rates",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert issue_weekly_rates.status_code == 200
    issue_weekly_rates_body = issue_weekly_rates.json()
    assert issue_weekly_rates_body["weeks"] == ["2026-04-06"]
    assert [row["case_name"] for row in issue_weekly_rates_body["rows"]] == [
        "TestCaseAlpha",
        "TestCaseBeta",
    ]
    assert issue_weekly_rates_body["rows"][0]["cells"] == ["100.00% (1/1)"]
    assert issue_weekly_rates_body["rows"][1]["cells"] == ["100.00% (1/1)"]
    assert issue_weekly_rates_body["trend"]["series"] == [
        {
            "key": "issue_filtered_flaky_rate_pct",
            "type": "line",
            "points": [["2026-04-06", 100.0]],
        }
    ]

    issue_weekly_rates_open = api_client.get(
        "/api/v1/flaky/issue-weekly-rates",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "issue_status": "open",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert issue_weekly_rates_open.status_code == 200
    assert [row["case_name"] for row in issue_weekly_rates_open.json()["rows"]] == [
        "TestCaseAlpha",
    ]


def test_flaky_top_jobs_and_period_comparison(api_client: TestClient) -> None:
    top_jobs = api_client.get(
        "/api/v1/flaky/top-jobs",
        params={"repo": "pingcap/tidb", "branch": "master", "limit": 2},
    )
    assert top_jobs.status_code == 200
    top_jobs_body = top_jobs.json()
    assert [item["name"] for item in top_jobs_body["items"]] == ["job-a", "job-c"]
    assert top_jobs_body["items"][0]["value"] == 2
    assert top_jobs_body["items"][0]["noisy_rate_pct"] == 100.0
    assert top_jobs_body["meta"]["limit"] == 2

    period_comparison = api_client.get(
        "/api/v1/flaky/period-comparison",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "period_a_start": "2026-04-10",
            "period_a_end": "2026-04-10",
            "period_b_start": "2026-04-11",
            "period_b_end": "2026-04-11",
        },
    )
    assert period_comparison.status_code == 200
    groups = {
        group["name"]: group["values"]
        for group in period_comparison.json()["groups"]
    }
    assert groups["period_a"] == {
        "total_build_count": 3,
        "failure_like_build_count": 2,
        "flaky_build_count": 1,
        "retry_loop_build_count": 1,
        "noisy_build_count": 2,
        "total_pr_count": 2,
        "affected_pr_count": 1,
        "flaky_rate_pct": 50.0,
        "retry_loop_rate_pct": 50.0,
        "noisy_rate_pct": 100.0,
        "affected_pr_rate_pct": 50.0,
    }
    assert groups["period_b"] == {
        "total_build_count": 2,
        "failure_like_build_count": 2,
        "flaky_build_count": 1,
        "retry_loop_build_count": 0,
        "noisy_build_count": 1,
        "total_pr_count": 1,
        "affected_pr_count": 1,
        "flaky_rate_pct": 50.0,
        "retry_loop_rate_pct": 0.0,
        "noisy_rate_pct": 50.0,
        "affected_pr_rate_pct": 100.0,
    }


def test_case_tables_exclude_cross_cloud_and_stale_build_key_collisions(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        source_prow_row_id=900,
        source_prow_job_id="live-build",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="collision-job",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 12:00:00",
        pr_number=900,
        normalized_build_key="/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/944",
        build_id="prow-live",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=900,
        target_branch="master",
        event_key="pr-900-master",
        event_time="2026-04-10 11:55:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestLive",
        build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/944/",
        flaky=1,
        report_time="2026-04-10 12:10:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70001,
        case_name="TestLive",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:00:00",
        issue_closed_at="2026-04-10 20:00:00",
    )

    _insert_build(
        sqlite_engine,
        source_prow_row_id=901,
        source_prow_job_id="ghost-cloud-build",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="collision-job",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 13:00:00",
        pr_number=901,
        normalized_build_key="/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/943",
        build_id="prow-ghost-cloud",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=901,
        target_branch="master",
        event_key="pr-901-master",
        event_time="2026-04-10 12:55:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestGhostCloud",
        build_url="https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/943/",
        flaky=1,
        report_time="2025-07-25 04:01:13",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70002,
        case_name="TestGhostCloud",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:10:00",
        issue_closed_at="2026-04-10 20:10:00",
    )

    _insert_build(
        sqlite_engine,
        source_prow_row_id=902,
        source_prow_job_id="ghost-time-build",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="collision-job",
        state="success",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 14:00:00",
        pr_number=902,
        normalized_build_key="/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/945",
        build_id="prow-ghost-time",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=902,
        target_branch="master",
        event_key="pr-902-master",
        event_time="2026-04-10 13:55:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestGhostTime",
        build_url="https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/945/",
        flaky=1,
        report_time="2025-07-24 04:01:13",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70003,
        case_name="TestGhostTime",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:20:00",
        issue_closed_at="2026-04-10 20:20:00",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            distinct_counts = client.get(
                "/api/v1/flaky/distinct-case-counts",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-10",
                    "end_date": "2026-04-11",
                },
            )
            assert distinct_counts.status_code == 200
            assert distinct_counts.json()["rows"] == [{"branch": "master", "values": [1]}]

            issue_weekly_rates = client.get(
                "/api/v1/flaky/issue-weekly-rates",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "issue_status": "closed",
                    "start_date": "2026-04-10",
                    "end_date": "2026-04-11",
                },
            )
            assert issue_weekly_rates.status_code == 200
            rows = {
                row["case_name"]: row
                for row in issue_weekly_rates.json()["rows"]
            }
            assert rows["TestLive"]["cells"] == ["50.00% (1/2)"]
            assert rows["TestGhostCloud"]["cells"] == ["0.00% (0/0)"]
            assert rows["TestGhostTime"]["cells"] == ["0.00% (0/0)"]
            assert issue_weekly_rates.json()["trend"]["series"] == [
                {
                    "key": "issue_filtered_flaky_rate_pct",
                    "type": "line",
                    "points": [["2026-04-06", 50.0]],
                }
            ]
    finally:
        app.dependency_overrides.clear()


def test_flaky_validation_errors(api_client: TestClient) -> None:
    invalid_granularity = api_client.get(
        "/api/v1/flaky/trend",
        params={"granularity": "month"},
    )
    assert invalid_granularity.status_code == 400
    assert invalid_granularity.json()["detail"] == "granularity must be one of: day, week"

    invalid_range = api_client.get(
        "/api/v1/flaky/composition",
        params={"start_date": "2026-04-12", "end_date": "2026-04-11"},
    )
    assert invalid_range.status_code == 400
    assert invalid_range.json()["detail"] == "start_date must be on or before end_date"

    invalid_limit = api_client.get(
        "/api/v1/flaky/top-jobs",
        params={"limit": 0},
    )
    assert invalid_limit.status_code == 400
    assert invalid_limit.json()["detail"] == "limit must be positive"

    invalid_issue_status = api_client.get(
        "/api/v1/flaky/issue-weekly-rates",
        params={"issue_status": "reopened"},
    )
    assert invalid_issue_status.status_code == 400
    assert invalid_issue_status.json()["detail"] == "issue_status must be one of: open, closed"


def test_build_routes(api_client: TestClient) -> None:
    outcome_trend = api_client.get(
        "/api/v1/builds/outcome-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert outcome_trend.status_code == 200
    outcome_series = {item["key"]: item for item in outcome_trend.json()["series"]}
    assert outcome_series["total_count"]["points"] == [["2026-04-10", 3], ["2026-04-11", 2]]
    assert outcome_series["success_rate_pct"]["axis"] == "right"
    assert outcome_series["success_rate_pct"]["points"] == [["2026-04-10", 33.33], ["2026-04-11", 0.0]]

    duration_trend = api_client.get(
        "/api/v1/builds/duration-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert duration_trend.status_code == 200
    duration_body = duration_trend.json()
    duration_series = {item["key"]: item for item in duration_body["series"]}
    assert duration_series["queue_avg_s"]["points"] == [["2026-04-10", 120], ["2026-04-11", 0]]
    assert duration_series["run_avg_s"]["points"] == [["2026-04-10", 600], ["2026-04-11", 0]]
    assert duration_series["total_avg_s"]["points"] == [["2026-04-10", 720], ["2026-04-11", 0]]
    assert duration_body["meta"]["summary"] == {
        "queue_avg_s": 120,
        "run_avg_s": 600,
        "total_avg_s": 720,
    }

    cloud_comparison = api_client.get(
        "/api/v1/builds/cloud-comparison",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert cloud_comparison.status_code == 200
    cloud_groups = {item["name"]: item["metrics"] for item in cloud_comparison.json()["groups"]}
    assert cloud_groups["GCP"]["total_builds"] == 4
    assert cloud_groups["IDC"]["total_builds"] == 1
    assert cloud_groups["IDC"]["success_rate_pct"] == 0.0
    assert cloud_groups["GCP"]["queue_avg_s"] == 120
    assert cloud_groups["GCP"]["run_avg_s"] == 600
    assert cloud_groups["GCP"]["total_avg_s"] == 720
    assert cloud_groups["IDC"]["queue_avg_s"] == 0

    migration_runtime = api_client.get(
        "/api/v1/builds/migration-runtime-comparison",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-15",
        },
    )
    assert migration_runtime.status_code == 200
    migration_body = migration_runtime.json()
    assert migration_body["meta"]["anchor_end_date"] == "2026-04-15"
    assert migration_body["meta"]["window_days"] == 14
    assert migration_body["meta"]["min_success_runs_each_side"] == 5
    assert migration_body["meta"]["improved_limit"] == 10
    assert migration_body["improved"] == [
        {
            "job_name": "job-fast",
            "normalized_job_path": "/jenkins/job/pingcap/job/tidb/job/job-fast",
            "idc_baseline_avg_run_s": 600,
            "gcp_recent_avg_run_s": 300,
            "delta_run_s": -300,
            "delta_pct": -50.0,
            "idc_success_count": 5,
            "gcp_success_count": 5,
            "first_gcp_success_at": "2026-03-01T09:00:00Z",
        }
    ]
    assert migration_body["regressed"] == [
        {
            "job_name": "job-slow",
            "normalized_job_path": "/jenkins/job/pingcap/job/tidb/job/job-slow",
            "idc_baseline_avg_run_s": 200,
            "gcp_recent_avg_run_s": 500,
            "delta_run_s": 300,
            "delta_pct": 150.0,
            "idc_success_count": 5,
            "gcp_success_count": 5,
            "first_gcp_success_at": "2026-03-01T10:00:00Z",
        }
    ]


def test_failure_routes(api_client: TestClient) -> None:
    category_trend = api_client.get(
        "/api/v1/failures/category-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert category_trend.status_code == 200
    category_series = {item["key"]: item for item in category_trend.json()["series"]}
    assert category_series["FLAKY_TEST"]["points"] == [["2026-04-10", 2], ["2026-04-11", 1]]
    assert category_series["UNCLASSIFIED"]["points"] == [["2026-04-10", 0], ["2026-04-11", 1]]

    category_share = api_client.get(
        "/api/v1/failures/category-share",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert category_share.status_code == 200
    category_share_body = category_share.json()
    assert category_share_body["categories"] == ["FLAKY_TEST", "UNCLASSIFIED"]
    share_groups = {item["name"]: item["values"] for item in category_share_body["groups"]}
    assert share_groups["GCP"] == [2, 1]
    assert share_groups["IDC"] == [1, 0]


def test_page_routes(api_client: TestClient) -> None:
    overview = api_client.get(
        "/api/v1/pages/overview",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert overview.status_code == 200
    overview_body = overview.json()
    assert overview_body["scope"]["repo"] == "pingcap/tidb"
    assert len(overview_body["repos"]["items"]) == 2
    assert overview_body["top_noisy_jobs"]["meta"]["limit"] == 5
    overview_groups = {
        group["name"]: group["values"]
        for group in overview_body["period_comparison"]["groups"]
    }
    assert overview_groups["period_a"]["total_build_count"] == 5
    assert overview_groups["period_a"]["noisy_build_count"] == 3
    assert overview_groups["period_b"]["total_build_count"] == 0

    build_trend = api_client.get(
        "/api/v1/pages/build-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-15",
        },
    )
    assert build_trend.status_code == 200
    build_trend_body = build_trend.json()
    assert build_trend_body["scope"]["branch"] == "master"
    cloud_posture_series = {
        item["key"]: item for item in build_trend_body["cloud_posture_trend"]["series"]
    }
    assert build_trend_body["cloud_posture_trend"]["meta"]["bucket_granularity"] == "week"
    assert cloud_posture_series["gcp_build_count"]["points"] == [["2026-04-06", 4]]
    assert cloud_posture_series["idc_build_count"]["points"] == [["2026-04-06", 1]]
    assert build_trend_body["longest_avg_success_jobs"]["items"] == [
        {
            "name": "job-b",
            "value": 600,
            "total_build_count": 2,
            "success_build_count": 1,
            "success_rate_pct": 50.0,
            "job_url": "https://prow.tidb.net/jenkins/job/job-4",
        }
    ]
    assert build_trend_body["lowest_success_rate_jobs"]["items"] == [
        {
            "name": "job-a",
            "value": 0.0,
            "total_build_count": 2,
            "success_build_count": 0,
            "success_avg_run_s": 0,
            "job_url": "https://do.pingcap.net/jenkins/job/job-2",
        },
        {
            "name": "job-c",
            "value": 0.0,
            "total_build_count": 1,
            "success_build_count": 0,
            "success_avg_run_s": 0,
            "job_url": "https://prow.tidb.net/jenkins/job/job-5",
        },
        {
            "name": "job-b",
            "value": 50.0,
            "total_build_count": 2,
            "success_build_count": 1,
            "success_avg_run_s": 600,
            "job_url": "https://prow.tidb.net/jenkins/job/job-4",
        },
    ]
    assert build_trend_body["migration_runtime_comparison"]["improved"][0]["job_name"] == "job-fast"
    assert build_trend_body["migration_runtime_comparison"]["regressed"][0]["job_name"] == "job-slow"
    assert build_trend_body["migration_runtime_comparison"]["meta"]["improved_limit"] == 10

    build_trend_all = api_client.get(
        "/api/v1/pages/build-trend",
        params={"start_date": "2026-04-10", "end_date": "2026-04-11"},
    )
    assert build_trend_all.status_code == 200
    build_trend_all_body = build_trend_all.json()
    cloud_repo_share = {
        item["cloud_phase"]: item for item in build_trend_all_body["cloud_repo_share"]["clouds"]
    }
    assert cloud_repo_share["GCP"]["total_builds"] == 4
    assert cloud_repo_share["GCP"]["items"] == [
        {
            "name": "pingcap/tidb",
            "value": 4,
            "share_pct": 100.0,
            "branches": [{"name": "master", "value": 4, "share_pct": 100.0}],
        }
    ]
    assert cloud_repo_share["IDC"]["total_builds"] == 3
    assert cloud_repo_share["IDC"]["items"] == [
        {
            "name": "pingcap/tidb",
            "value": 2,
            "share_pct": 66.67,
            "branches": [
                {"name": "main", "value": 1, "share_pct": 50.0},
                {"name": "master", "value": 1, "share_pct": 50.0},
            ],
        },
        {
            "name": "pingcap/tiflash",
            "value": 1,
            "share_pct": 33.33,
            "branches": [{"name": "release-8.5", "value": 1, "share_pct": 100.0}],
        },
    ]

    build_trend_repo_filtered = api_client.get(
        "/api/v1/pages/build-trend",
        params={
            "repo": "pingcap/tidb",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert build_trend_repo_filtered.status_code == 200
    build_trend_repo_filtered_body = build_trend_repo_filtered.json()
    cloud_repo_share_with_repo_filter = {
        item["cloud_phase"]: item
        for item in build_trend_repo_filtered_body["cloud_repo_share"]["clouds"]
    }
    assert cloud_repo_share_with_repo_filter["GCP"]["total_builds"] == 4
    assert cloud_repo_share_with_repo_filter["IDC"]["total_builds"] == 3
    assert [item["name"] for item in cloud_repo_share_with_repo_filter["IDC"]["items"]] == [
        "pingcap/tidb",
        "pingcap/tiflash",
    ]

    build_trend_cloud_filtered = api_client.get(
        "/api/v1/pages/build-trend",
        params={
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
            "cloud_phase": "GCP",
        },
    )
    assert build_trend_cloud_filtered.status_code == 200
    build_trend_cloud_filtered_body = build_trend_cloud_filtered.json()
    cloud_repo_share_with_cloud_filter = {
        item["cloud_phase"]: item
        for item in build_trend_cloud_filtered_body["cloud_repo_share"]["clouds"]
    }
    assert build_trend_cloud_filtered_body["cloud_posture_trend"]["series"] == [
        {
            "key": "gcp_build_count",
            "label": "GCP builds",
            "type": "bar",
            "points": [["2026-04-06", 4]],
        },
        {
            "key": "idc_build_count",
            "label": "IDC builds",
            "type": "bar",
            "points": [["2026-04-06", 0]],
        },
    ]
    assert cloud_repo_share_with_cloud_filter["GCP"]["total_builds"] == 4
    assert cloud_repo_share_with_cloud_filter["IDC"]["total_builds"] == 3

    flaky = api_client.get(
        "/api/v1/pages/flaky",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert flaky.status_code == 200
    flaky_body = flaky.json()
    assert flaky_body["top_jobs"]["meta"]["limit"] == 8
    assert flaky_body["distinct_flaky_case_counts"]["weeks"] == ["2026-04-06"]
    assert flaky_body["distinct_flaky_case_counts"]["rows"] == [
        {"branch": "master", "values": [2]}
    ]
    assert flaky_body["issue_filtered_weekly_trend"]["series"] == [
        {
            "key": "issue_filtered_flaky_rate_pct",
            "type": "line",
            "points": [["2026-04-06", 100.0]],
        }
    ]
    assert flaky_body["issue_case_weekly_rates"]["rows"][0]["display_name"] == "TestCaseAlpha"
    assert flaky_body["issue_case_weekly_rates"]["rows"][0]["cells"] == ["100.00% (1/1)"]
    assert flaky_body["issue_case_weekly_rates"]["rows"][1]["cells"] == ["100.00% (1/1)"]
    failure_series = {
        item["key"]: item
        for item in flaky_body["failure_category_trend"]["series"]
    }
    assert failure_series["FLAKY_TEST"]["points"] == [["2026-04-10", 2], ["2026-04-11", 1]]
    period_groups = {
        group["name"]: group["values"]
        for group in flaky_body["period_comparison"]["groups"]
    }
    assert period_groups["period_a"]["total_pr_count"] == 3
    assert period_groups["period_a"]["affected_pr_count"] == 2
    assert period_groups["period_a"]["affected_pr_rate_pct"] == 66.67

    flaky_with_open_issues = api_client.get(
        "/api/v1/pages/flaky",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "issue_status": "open",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert flaky_with_open_issues.status_code == 200
    flaky_with_open_issues_body = flaky_with_open_issues.json()
    assert [row["case_name"] for row in flaky_with_open_issues_body["issue_case_weekly_rates"]["rows"]] == [
        "TestCaseAlpha"
    ]
    assert flaky_with_open_issues_body["trend"] == flaky_body["trend"]
    assert flaky_with_open_issues_body["composition"] == flaky_body["composition"]
    assert flaky_with_open_issues_body["top_jobs"] == flaky_body["top_jobs"]
    assert flaky_with_open_issues_body["failure_category_share"] == flaky_body["failure_category_share"]
    assert flaky_with_open_issues_body["failure_category_trend"] == flaky_body["failure_category_trend"]
    assert flaky_with_open_issues_body["period_comparison"] == flaky_body["period_comparison"]
