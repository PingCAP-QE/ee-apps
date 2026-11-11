from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.main import app
from ci_dashboard.api.queries import flaky as flaky_queries
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.flaky import get_flaky_top_cases


WINDOW = {"start_date": "2026-10-29", "end_date": "2026-11-11"}
FILTERS = CommonFilters(
    repo="pingcap/tidb", branch="release-8.5",
    start_date=date(2026, 10, 29), end_date=date(2026, 11, 11),
)


def _report(engine, case="TestHot", build="1", **overrides):
    values = {
        "repo": "pingcap/tidb", "branch": "release-8.5", "case_name": case,
        "suite_name": "unit", "flaky": 1, "report_time": "2026-11-01 12:00:00",
        "build_url": f"https://prow.tidb.net/jenkins/job/unit/{build}/",
        "normalized_build_key": None, "cloud_phase": None,
        **overrides,
    }
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO problem_case_runs
              (repo, branch, case_name, suite_name, flaky, report_time, build_url,
               normalized_build_key, cloud_phase)
            VALUES (:repo, :branch, :case_name, :suite_name, :flaky, :report_time,
                    :build_url, :normalized_build_key, :cloud_phase)
        """), values)


def _issue(engine, number, case="TestHot", repo="pingcap/tidb", status="open", branch="master"):
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO ci_l1_flaky_issues
              (repo, issue_number, issue_url, issue_title, case_name, issue_status,
               issue_branch, issue_created_at, issue_updated_at, issue_closed_at,
               source_ticket_id, source_ticket_updated_at)
            VALUES (:repo, :number, :url, :title, :case, :status, :branch,
                    '2025-01-01', '2026-10-01', '2026-10-01', :number, '2026-10-01')
        """), {"repo": repo, "number": number, "url": f"https://github.com/{repo}/issues/{number}",
                 "title": f"Flaky test: {case}", "case": case, "status": status, "branch": branch})


@pytest.fixture()
def client(sqlite_engine):
    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_engine, None)


@pytest.mark.parametrize("cloud_params", [{}, {"cloud_phase": ""}])
def test_top_cases_deduplicate_builds_and_apply_report_scope(sqlite_engine, client, cloud_params):
    _report(sqlite_engine, build="1", report_time="2026-10-29 00:00:00")
    _report(sqlite_engine, build="1", report_time="2026-11-01 12:30:00", suite_name="other")
    _report(sqlite_engine, build_url="https://prow.tidb.net/jenkins/job/unit/1/display/redirect")
    _report(sqlite_engine, build="2", report_time="2026-11-11 23:59:59")
    _report(sqlite_engine, case="TestNoIssue", build="3")
    for overrides in (
        {"repo": "tikv/pd"}, {"branch": "master"}, {"flaky": 0},
        {"report_time": "2026-10-28 23:59:59"},
        {"report_time": "2026-11-12 00:00:00"}, {"build_url": ""}, {"case_name": ""},
    ):
        _report(sqlite_engine, build="excluded", **overrides)

    response = client.get("/api/v1/flaky/top-cases", params={
        **WINDOW, "repo": "pingcap/tidb", "branch": "release-8.5", **cloud_params,
    })
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [(r["case_name"], r["affected_build_count"]) for r in rows] == [
        ("TestHot", 2), ("TestNoIssue", 1),
    ]
    assert rows[0]["builds"] == [
        {
            "build_url": "https://prow.tidb.net/jenkins/job/unit/2/",
            "cloud_phase": "GCP",
            "last_report_time": "2026-11-11 23:59:59",
        },
        {
            "build_url": "https://prow.tidb.net/jenkins/job/unit/1/",
            "cloud_phase": "GCP",
            "last_report_time": "2026-11-01 12:30:00",
        },
    ]
    assert response.json()["meta"]["time_basis"] == "report_time"


def test_top_cases_expose_build_links_on_the_reporting_controller(sqlite_engine):
    prow = "https://prow.tidb.net/jenkins/job/unit/7/"
    tencent_legacy = "https://do.pingcap.net/jenkins/job/unit/7/"
    _report(sqlite_engine, build_url=prow, normalized_build_key=prow, cloud_phase="GCP")
    # A TENCENT report may carry a GCP-formatted key; the link must stay on its controller.
    _report(sqlite_engine, build_url=tencent_legacy, normalized_build_key=prow,
            cloud_phase="TENCENT", report_time="2026-11-02 00:00:00")
    _report(sqlite_engine, build_url=tencent_legacy, normalized_build_key=tencent_legacy,
            cloud_phase="TENCENT", report_time="2026-11-02 00:00:00")
    builds = get_flaky_top_cases(sqlite_engine, FILTERS)["rows"][0]["builds"]
    assert builds == [
        {
            "build_url": tencent_legacy,
            "cloud_phase": "TENCENT",
            "last_report_time": "2026-11-02 00:00:00",
        },
        {
            "build_url": prow,
            "cloud_phase": "GCP",
            "last_report_time": "2026-11-01 12:00:00",
        },
    ]


def test_top_cases_cap_build_links_without_changing_the_count(sqlite_engine):
    for index in range(60):
        _report(sqlite_engine, build=f"cap-{index}", report_time=f"2026-11-01 12:{index:02d}:00")
    row = get_flaky_top_cases(sqlite_engine, FILTERS)["rows"][0]
    assert row["affected_build_count"] == 60
    assert len(row["builds"]) == flaky_queries.MAX_CASE_BUILD_LINKS == 10
    assert row["builds"][0]["last_report_time"] == "2026-11-01 12:59:00"
    assert row["builds"][-1]["last_report_time"] == "2026-11-01 12:50:00"


def test_top_cases_link_exotic_keys_to_the_reporting_controller(sqlite_engine):
    _report(
        sqlite_engine,
        build_url="https://jenkins.jenkins.svc.cluster.local/jenkins/job/unit/9/",
        normalized_build_key="http://jenkins.jenkins.svc.cluster.local/jenkins/job/unit/9/",
        cloud_phase="TENCENT",
    )
    builds = get_flaky_top_cases(sqlite_engine, FILTERS)["rows"][0]["builds"]
    assert builds[0]["build_url"] == "https://do.pingcap.net/jenkins/job/unit/9/"


def test_top_cases_keep_closed_and_cross_branch_issues_without_filtering_ranking(sqlite_engine, client):
    _report(sqlite_engine)
    _report(sqlite_engine, case="TestNoIssue", build="2")
    _issue(sqlite_engine, 101, status="closed")
    _issue(sqlite_engine, 100, status="open", branch="release-8.5")
    _issue(sqlite_engine, 200, repo="tikv/pd")
    _issue(sqlite_engine, 300, case="TestOther")

    data = client.get("/api/v1/flaky/top-cases", params={
        **WINDOW, "repo": "pingcap/tidb", "branch": "release-8.5", "issue_status": "closed",
    }).json()
    assert len(data["rows"]) == 2
    assert [i["issue_number"] for i in data["rows"][0]["issues"]] == [100, 101]
    assert data["rows"][0]["issues"][1]["issue_status"] == "closed"
    assert data["rows"][1]["issues"] == []
    assert data["meta"]["ignores_issue_status"] is True
    assert data["meta"]["ignores_issue_branch"] is True


def test_top_cases_limit_and_ties_are_stable_and_all_repos_stay_separate(sqlite_engine):
    for index in reversed(range(12)):
        _report(sqlite_engine, case=f"Test{index:02d}")
    _report(sqlite_engine, case="Test00", build="pd1", repo="tikv/pd", branch="master")
    _report(sqlite_engine, case="Test00", build="pd2", repo="tikv/pd", branch="master")
    data = get_flaky_top_cases(sqlite_engine, CommonFilters(
        start_date=FILTERS.start_date, end_date=FILTERS.end_date,
    ))
    assert len(data["rows"]) == 10
    assert data["rows"][0]["repo"] == "tikv/pd"
    assert data["rows"][0]["case_name"] == "Test00"
    assert data["rows"][0]["affected_build_count"] == 2
    assert [r["case_name"] for r in data["rows"][1:]] == [f"Test{i:02d}" for i in range(9)]
    assert data["meta"]["limit"] == 10


@pytest.mark.parametrize("cloud,expected", [(None, 2), ("", 2), ("GCP", 1), ("TENCENT", 1)])
def test_top_cases_distinguish_controllers_and_deduplicate_legacy_keys(sqlite_engine, cloud, expected):
    gcp = "https://prow.tidb.net/jenkins/job/unit/1/"
    tencent = "https://do.pingcap.net/jenkins/job/unit/1/"
    _report(sqlite_engine, build_url=gcp, normalized_build_key=gcp, cloud_phase="GCP")
    _report(sqlite_engine, build_url=tencent, normalized_build_key=gcp, cloud_phase="TENCENT")
    _report(sqlite_engine, build_url=tencent, normalized_build_key=tencent, cloud_phase="TENCENT")
    data = get_flaky_top_cases(sqlite_engine, CommonFilters(
        repo=FILTERS.repo, branch=FILTERS.branch, start_date=FILTERS.start_date,
        end_date=FILTERS.end_date, cloud_phase=cloud,
    ))
    assert data["rows"][0]["affected_build_count"] == expected


def test_top_cases_job_filter_uses_actual_trigger_not_testcase_repo_branch_or_build_result(sqlite_engine):
    url = "https://do.pingcap.net/jenkins/job/pd-tests/1/"
    _report(sqlite_engine, build_url=url, cloud_phase="TENCENT", report_time="2026-10-29 00:30:00")
    _report(sqlite_engine, case="TooLate", build_url=url, cloud_phase="TENCENT",
            report_time="2026-10-31 00:30:00")
    _report(sqlite_engine, case="WrongCloud", build_url=url, cloud_phase="GCP")
    _report(sqlite_engine, case="NoMatchingBuild", build="404")
    with sqlite_engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO ci_l1_builds
              (repo_full_name, target_branch, normalized_build_url, cloud_phase,
               job_name, start_time, state)
            VALUES ('tikv/pd', 'master', :url, 'TENCENT', 'pd-unit',
                    '2026-10-28 23:30:00', 'success')
        """), {"url": url})
    data = get_flaky_top_cases(sqlite_engine, CommonFilters(
        repo=FILTERS.repo, branch=FILTERS.branch, start_date=FILTERS.start_date,
        end_date=FILTERS.end_date, job_name="pd-unit,other-job", cloud_phase="TENCENT",
    ))
    assert (data["rows"][0]["case_name"], data["rows"][0]["affected_build_count"]) == (
        "TestHot", 1,
    )
    assert [b["build_url"] for b in data["rows"][0]["builds"]] == [url]


@pytest.mark.parametrize("scope", [
    {}, {"repo": "pingcap/tidb"}, {"branch": "release-8.5"},
])
def test_top_cases_unpinned_scopes_use_report_time_index(sqlite_engine, scope):
    migration = Path(__file__).resolve().parents[2] / "sql/033_add_flaky_report_time_index.sql"
    with sqlite_engine.begin() as connection:
        # Reproduce the repository's pre-existing index order, without MySQL prefixes.
        connection.exec_driver_sql("""
            CREATE INDEX idx_problem_case_runs_flaky_repo_key_time
            ON problem_case_runs (flaky, repo, normalized_build_key, report_time)
        """)
        connection.exec_driver_sql("""
            CREATE INDEX idx_problem_case_runs_repo_branch_time_case
            ON problem_case_runs (repo, branch, report_time, case_name)
        """)
        connection.exec_driver_sql(migration.read_text())
        connection.exec_driver_sql(migration.read_text())  # Safe to reapply.
    _report(sqlite_engine)
    _report(sqlite_engine, case="OutsideWindow", report_time="2025-01-01 00:00:00")
    statements = []

    def record(_connection, _cursor, statement, params, _context, _executemany):
        statements.append((statement, params))

    event.listen(sqlite_engine, "before_cursor_execute", record)
    try:
        data = get_flaky_top_cases(sqlite_engine, CommonFilters(
            start_date=FILTERS.start_date, end_date=FILTERS.end_date, **scope,
        ))
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", record)
    assert [row["case_name"] for row in data["rows"]] == ["TestHot"]
    statement, params = statements[0]
    # Planner-agnostic guard: the report-time window is a SQL predicate, not a
    # post-filter, and both bounds stay bound parameters for every scope.
    assert "pcr.report_time >=" in statement
    assert "pcr.report_time <" in statement


def test_top_cases_empty_scope_and_default_window(client):
    response = client.get("/api/v1/flaky/top-cases")
    assert response.status_code == 200
    assert response.json()["rows"] == []
    meta = response.json()["meta"]
    assert (date.fromisoformat(meta["end_date"]) - date.fromisoformat(meta["start_date"])).days == 29


@pytest.mark.parametrize("params", [
    {"start_date": "2026-10-29"},
    {"start_date": "2025-11-11", "end_date": "2026-11-11"},
    {**WINDOW, "job_name": ",".join(["unit"] * 101)},
])
def test_top_cases_validate_before_reading_database(sqlite_engine, client, params):
    reads = []

    def record(_connection, _cursor, statement, _params, _context, _executemany):
        reads.append(statement)

    event.listen(sqlite_engine, "before_cursor_execute", record)
    try:
        assert client.get("/api/v1/flaky/top-cases", params=params).status_code == 422
        assert reads == []
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", record)
