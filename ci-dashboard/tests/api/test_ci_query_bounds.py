from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.main import create_app
from ci_dashboard.api.routes import common


CI_PATHS = [
    "/api/v1/builds/outcome-trend",
    "/api/v1/builds/duration-trend",
    "/api/v1/builds/cloud-comparison",
    "/api/v1/builds/migration-runtime-comparison",
    "/api/v1/failures/category-trend",
    "/api/v1/failures/category-share",
    "/api/v1/flaky/trend",
    "/api/v1/flaky/composition",
    "/api/v1/flaky/distinct-case-counts",
    "/api/v1/flaky/case-flow-v2",
    "/api/v1/flaky/issue-weekly-rates",
    "/api/v1/flaky/top-jobs",
    "/api/v1/pages/overview",
    "/api/v1/pages/ci-status",
    "/api/v1/pages/flaky",
    "/api/v1/pages/runtime-insights",
    "/api/v1/pages/runtime-error-top-jobs",
    "/api/v1/pages/runtime-error-builds",
    "/api/v1/filters/repos",
    "/api/v1/filters/branches",
    "/api/v1/filters/jobs",
    "/api/v1/filters/cloud-phases",
]
LIST_FIELDS = [
    "job_name", "cost_source", "owner_include", "owner_exclude",
    "team_include", "team_exclude", "project_include", "project_exclude",
]
RANGE = {"start_date": "2026-04-01", "end_date": "2026-04-30"}


@pytest.fixture()
def bounded_client(sqlite_engine):
    app = create_app()
    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def sql_statements(sqlite_engine):
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(sqlite_engine, "before_cursor_execute", record)
    yield statements
    event.remove(sqlite_engine, "before_cursor_execute", record)


@pytest.mark.parametrize("path", CI_PATHS)
def test_ci_date_budget_rejects_before_sql(path, bounded_client, sql_statements):
    for params in [
        {"start_date": "2026-04-01"},
        {"end_date": "2026-04-30"},
        *[
            {"start_date": "2026-01-01", "end_date": "2027-01-01", "granularity": unit}
            for unit in ("day", "week", "month")
        ],
        {"start_date": "1969-12-31", "end_date": "1970-01-01"},
        {"start_date": "9999-12-01", "end_date": "9999-12-25"},
        {"start_date": "9999-12-01", "end_date": "9999-12-31"},
    ]:
        response = bounded_client.get(path, params=params)
        assert response.status_code == 422, (path, params, response.text)
    assert sql_statements == []


@pytest.mark.parametrize(("path", "start", "end"), [
    ("/api/v1/flaky/case-flow-v2", "9999-10-02", "9999-12-30"),
    ("/api/v1/flaky/issue-weekly-rates", "9999-10-02", "9999-12-30"),
    ("/api/v1/flaky/distinct-case-counts", "9999-12-01", "9999-12-27"),
    ("/api/v1/pages/overview", "0001-01-01", "0001-01-31"),
    ("/api/v1/pages/flaky", "0001-03-01", "0001-05-29"),
])
def test_ci_extreme_dates_reject_before_sql(
    path, start, end, bounded_client, sql_statements,
):
    response = bounded_client.get(path, params={"start_date": start, "end_date": end})
    assert response.status_code == 422
    assert sql_statements == []


@pytest.mark.parametrize("path", [
    "/api/v1/pages/overview", "/api/v1/pages/flaky",
    "/api/v1/flaky/distinct-case-counts", "/api/v1/flaky/case-flow-v2",
    "/api/v1/flaky/issue-weekly-rates",
])
@pytest.mark.parametrize(("start", "end"), [
    ("1970-01-01", "1970-12-31"),
    ("9998-12-25", "9999-12-24"),
])
def test_ci_safe_date_boundaries_are_accepted(path, start, end, bounded_client):
    response = bounded_client.get(path, params={"start_date": start, "end_date": end})
    assert response.status_code == 200


@pytest.mark.parametrize("path", CI_PATHS)
def test_ci_list_budget_rejects_before_sql(path, bounded_client, sql_statements):
    for field in LIST_FIELDS:
        for value in [
            ",".join(f"v{i}" for i in range(101)), "x" * 8193, "密" * 2731, "x," * 100,
        ]:
            response = bounded_client.get(path, params={**RANGE, field: value})
            assert response.status_code == 422, (path, field, response.text)
    assert sql_statements == []


def test_ci_undated_requests_default_to_thirty_utc_days(bounded_client, monkeypatch):
    monkeypatch.setattr(
        common, "utcnow", lambda: datetime(2026, 4, 12, 23, 59, tzinfo=UTC), raising=False,
    )
    response = bounded_client.get("/api/v1/builds/outcome-trend")
    assert response.status_code == 200
    assert response.json()["meta"]["start_date"] == "2026-03-14"
    assert response.json()["meta"]["end_date"] == "2026-04-12"


@pytest.mark.parametrize("unit", ["day", "week", "month"])
def test_ci_calendar_year_boundary_and_normal_filter_semantics(unit, bounded_client):
    response = bounded_client.get(
        "/api/v1/builds/outcome-trend",
        params={
            "start_date": "2024-02-01", "end_date": "2025-01-31", "granularity": unit,
            "job_name": ",".join(f"job-{i}" for i in range(100)),
            "owner_include": "alice, alice, bob",
        },
    )
    assert response.status_code == 200
    assert response.json()["meta"]["start_date"] == "2024-02-01"
    assert len(response.json()["meta"]["job_names"]) == 100


@pytest.mark.parametrize("path", CI_PATHS)
def test_ci_routes_allow_cost_navigation_ranges_beyond_ninety_days(path, bounded_client):
    # The existing navigation carries a valid multi-month Cost range into CI unchanged.
    for start, end in [("2026-01-01", "2026-09-30"), ("2024-02-01", "2025-01-31")]:
        for unit in ("day", "week", "month"):
            response = bounded_client.get(
                path, params={"start_date": start, "end_date": end, "granularity": unit},
            )
            assert response.status_code == 200, (path, unit, start, end, response.text)


def test_ci_normalized_lists_preserve_trimming_and_deduplication():
    filters = common.get_common_filters(
        granularity="day", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30),
        job_name="job-a, job-a,job-b", owner_include=" alice,alice,bob ",
    )
    assert filters.job_names == ("job-a", "job-b")
    assert filters.owner_include == ("alice", "bob")


@pytest.mark.parametrize("field", LIST_FIELDS)
def test_ci_list_character_budget_allows_exact_boundary(field):
    value = "gcp:" + "x" * 8188 if field == "cost_source" else "x" * 8192
    common.get_common_filters(
        granularity="day", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30),
        **{field: value},
    )


def test_filter_byte_budget_counts_utf8_not_characters():
    common.validate_filter_limits(job_name="密" * 2730)
    with pytest.raises(HTTPException) as error:
        common.validate_filter_limits(job_name="密" * 2731)
    assert error.value.status_code == 422


@pytest.mark.parametrize("path", [
    "/api/v1/filters/repos", "/api/v1/filters/branches",
    "/api/v1/filters/jobs", "/api/v1/filters/cloud-phases",
])
def test_filter_option_queries_apply_the_date_window(path, bounded_client, sql_statements):
    response = bounded_client.get(path, params=RANGE)
    assert response.status_code == 200
    assert sql_statements
    assert all("b.start_time >=" in sql and "b.start_time <" in sql for sql in sql_statements)


@pytest.mark.parametrize("period", ["a", "b"])
def test_period_comparison_has_no_date_or_job_list_bypass(
    period, bounded_client, sql_statements,
):
    params = {
        "period_a_start": "2026-04-01", "period_a_end": "2026-04-30",
        "period_b_start": "2026-03-01", "period_b_end": "2026-03-30",
    }
    for start, end in [
        ("2020-01-01", "2026-04-30"),
        ("2025-04-01", "2026-04-01"),
        ("1969-12-31", "1970-01-01"),
        ("9999-12-01", "9999-12-25"),
    ]:
        response = bounded_client.get(
            "/api/v1/flaky/period-comparison",
            params={**params, f"period_{period}_start": start, f"period_{period}_end": end},
        )
        assert response.status_code == 422
    response = bounded_client.get(
        "/api/v1/flaky/period-comparison",
        params={**params, "job_name": ",".join(f"job-{i}" for i in range(101))},
    )
    assert response.status_code == 422
    assert sql_statements == []


def test_period_comparison_accepts_two_calendar_years(bounded_client):
    response = bounded_client.get(
        "/api/v1/flaky/period-comparison",
        params={"period_a_start": "2024-02-01", "period_a_end": "2025-01-31",
                "period_b_start": "2023-02-01", "period_b_end": "2024-01-31"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("path", [
    "/api/v1/pages/cost", "/api/v1/pages/cost-filter-values", "/api/v1/pages/cost-trend",
    "/api/v1/pages/cost-share", "/api/v1/pages/cost-budget-pace",
    "/api/v1/pages/cost-weekly-account-summaries", "/api/v1/pages/cost-repo-group-stack",
    "/api/v1/pages/cost-engineering-group-share", "/api/v1/pages/cost-unmatched-resources",
])
def test_shared_cost_filter_lists_are_also_bounded(path, bounded_client, sql_statements):
    for field in LIST_FIELDS:
        response = bounded_client.get(
            path, params={"granularity": "month", field: ",".join(f"v{i}" for i in range(101))},
        )
        assert response.status_code == 422, (path, field, response.text)
    assert sql_statements == []


@pytest.mark.parametrize(("start", "end"), [
    (date(1970, 1, 1), date(1970, 12, 31)),
    (date(2024, 1, 1), date(2024, 12, 31)),
    (date(2024, 2, 29), date(2025, 2, 27)),
    (date(9998, 12, 25), date(9999, 12, 24)),
    (date(9999, 8, 1), date(9999, 12, 24)),
])
def test_ci_calendar_year_boundaries_include_leap_days_and_safe_max_year(start, end):
    filters = common.get_common_filters(granularity="day", start_date=start, end_date=end)
    assert filters.start_date == start
    assert filters.end_date == end


@pytest.mark.parametrize(("start", "end"), [
    (date(2025, 1, 1), date(2026, 1, 1)),
    (date(2024, 2, 1), date(2025, 2, 1)),
    (date(2024, 2, 29), date(2025, 2, 28)),
    (date(2025, 2, 28), date(2026, 2, 28)),
])
def test_ci_calendar_year_excess_rejects_before_sql(start, end, bounded_client, sql_statements):
    response = bounded_client.get(
        "/api/v1/builds/outcome-trend", params={"start_date": start, "end_date": end},
    )
    assert response.status_code == 422
    assert sql_statements == []
