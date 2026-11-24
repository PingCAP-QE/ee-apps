from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, text

from ci_dashboard.api.queries import flaky, pages
from ci_dashboard.api.queries.base import CommonFilters


FILTERS = CommonFilters(
    repo="pingcap/tidb", branch="master", issue_status="open",
    start_date=date(2026, 11, 16), end_date=date(2026, 11, 22), granularity="week",
)
MYSQL_ENGINE = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))


def _insert_issue(connection, number, name, created):
    connection.execute(text("""
        INSERT INTO ci_l1_flaky_issues
          (repo, issue_number, issue_branch, case_name, issue_status, issue_created_at,
           issue_updated_at, issue_url, issue_title, source_ticket_id, source_ticket_updated_at)
        VALUES ('pingcap/tidb', :number, 'master', :name, 'open', :created,
                '2026-11-20', 'https://github.com/pingcap/tidb/issues/1', 'flaky', :number, '2026-11-20')
    """), {"number": number, "name": name, "created": created})


def test_empty_issue_scope_skips_historical_query(sqlite_engine):
    with patch.object(flaky, "_fetch_issue_weekly_rate_rows", side_effect=AssertionError("historical scan")):
        result = flaky.get_issue_filtered_weekly_case_rates(sqlite_engine, FILTERS)
    assert result["rows"] == []
    assert result["weeks"] == ["2026-11-16"]
    assert result["trend"]["summary"][0]["cell"] == "N/A (0/0)"
    assert result["meta"]["issue_case_count"] == 0


@pytest.mark.parametrize("granularity,expected", [("week", 1), ("day", 2), ("month", 2)])
def test_chart_sections_reuse_raw_buckets_without_changing_partial_week_outputs(sqlite_engine, granularity, expected):
    filters = CommonFilters(
        repo="pingcap/tidb", start_date=date(2026, 11, 17), end_date=date(2026, 11, 22),
        granularity=granularity,
    )
    with sqlite_engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO ci_l1_builds (repo_full_name, job_name, state, start_time, is_flaky, cloud_phase)
            VALUES ('pingcap/tidb', 'unit', 'failure', '2026-11-18 10:00:00', 1, 'GCP')
        """))
    weekly_filters = replace(filters, granularity="week")
    original = {
        "trend": flaky.get_flaky_trend(sqlite_engine, filters),
        "composition": flaky.get_flaky_composition(sqlite_engine, weekly_filters),
        "bucketed_flaky_rate": flaky.get_flaky_bucketed_rate_view(sqlite_engine, filters),
    }
    with patch.object(flaky, "_query_bucketed_flaky_metrics", wraps=flaky._query_bucketed_flaky_metrics) as query:
        result = flaky.get_flaky_chart_sections(sqlite_engine, filters)
    assert result == original
    assert query.call_count == expected
    assert result["composition"]["series"][0]["points"] == []
    assert result["bucketed_flaky_rate"]["rows"]  # includes the partial week


@pytest.mark.parametrize("new_issue", [False, True])
def test_issue_sections_share_latest_rows_and_only_reuse_identical_link_keys(sqlite_engine, new_issue):
    with sqlite_engine.begin() as connection:
        _insert_issue(connection, 1, "TestOld", "2026-10-01")
        if new_issue:
            _insert_issue(connection, 2, "TestNew", "2026-11-20")
    original = {
        "issue_fix_progress": flaky.get_issue_fix_progress_snapshot(sqlite_engine, FILTERS),
        "issue_lifecycle": flaky.get_issue_lifecycle_snapshot(sqlite_engine, FILTERS),
        "issue_lifecycle_weekly": flaky.get_issue_lifecycle_weekly(sqlite_engine, FILTERS),
    }
    with (
        patch.object(flaky, "_fetch_issue_latest_rows", wraps=flaky._fetch_issue_latest_rows) as issues,
        patch.object(flaky, "_fetch_linked_pull_rows", wraps=flaky._fetch_linked_pull_rows) as pulls,
    ):
        assert flaky.get_issue_lifecycle_sections(sqlite_engine, FILTERS) == original
        assert issues.call_count == 1
        assert pulls.call_count == (2 if new_issue else 1)
        flaky.get_issue_lifecycle_sections(sqlite_engine, FILTERS)
        assert issues.call_count == 2, "reuse must not escape the request"


@pytest.mark.parametrize("granularity,count", [("week", 11), ("day", 12), ("month", 12)])
def test_whole_page_reuses_queries_and_matches_independent_sections(sqlite_engine, granularity, count):
    with sqlite_engine.begin() as connection:
        _insert_issue(connection, 1, "TestOld", "2026-10-01")
    filters = replace(FILTERS, granularity=granularity)
    statements = []

    def record(_connection, _cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    def independent_issues(engine, filters):
        return {
            "issue_fix_progress": flaky.get_issue_fix_progress_snapshot(engine, filters),
            "issue_lifecycle": flaky.get_issue_lifecycle_snapshot(engine, filters),
            "issue_lifecycle_weekly": flaky.get_issue_lifecycle_weekly(engine, filters),
        }

    def independent_charts(engine, filters):
        return {
            "trend": flaky.get_flaky_trend(engine, filters),
            "composition": flaky.get_flaky_composition(engine, replace(filters, granularity="week")),
            "bucketed_flaky_rate": flaky.get_flaky_bucketed_rate_view(engine, filters),
        }

    event.listen(sqlite_engine, "before_cursor_execute", record)
    try:
        actual = pages.get_flaky_page(sqlite_engine, filters)
        assert len(statements) == count
        statements.clear()
        with (
            patch.object(pages, "get_issue_lifecycle_sections", side_effect=independent_issues),
            patch.object(pages, "get_flaky_chart_sections", side_effect=independent_charts),
        ):
            expected = pages.get_flaky_page(sqlite_engine, filters)
        assert len(statements) == 15  # linked-PR reuse is also active in standalone calls
        assert actual == expected
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", record)


def test_page_task_window_is_bounded_and_success_order_is_stable():
    barrier = Barrier(4)
    lock = Lock()
    active = maximum = 0

    def task(index):
        def run():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if index < 4:
                    barrier.wait(timeout=5)
                return index
            finally:
                with lock:
                    active -= 1
        return run

    tasks = {str(index): task(index) for index in range(12)}
    result = pages._resolve_page_sections(MYSQL_ENGINE, tasks)
    assert list(result) == list(tasks)
    assert list(result.values()) == list(range(12))
    assert maximum == 4
    assert active == 0


def test_later_failure_is_observed_before_earlier_tasks_finish_and_queue_is_not_drained(monkeypatch):
    release = Event()
    observed = Event()
    lock = Lock()
    submitted = []
    error = RuntimeError("original SQL failure")

    class ObservedExecutor(ThreadPoolExecutor):
        def submit(self, fn):
            with lock:
                submitted.append(fn)
            future = super().submit(fn)
            result = future.result

            def observe(*args, **kwargs):
                try:
                    return result(*args, **kwargs)
                except RuntimeError:
                    observed.set()
                    raise
            future.result = observe
            return future

    def running():
        assert release.wait(timeout=10)

    def fail():
        raise error

    monkeypatch.setattr(pages, "ThreadPoolExecutor", ObservedExecutor)
    tasks = {"first": running, "second": running, "third": running, "later_failure": fail}
    tasks.update({str(index): lambda: None for index in range(8)})
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(pages._resolve_page_sections, MYSQL_ENGINE, tasks)
        try:
            assert observed.wait(timeout=5), "must observe failure without waiting for first future"
            assert len(submitted) == 4, "must not submit the remaining eight sections"
            assert not result.done(), "running SQL tasks must finish cleanup before returning"
        finally:
            release.set()
        with pytest.raises(RuntimeError) as raised:
            result.result(timeout=5)
        assert raised.value is error


@pytest.mark.parametrize("stored_key", [None, "", "https://prow.tidb.net/jenkins/job/unit/1/"])
def test_weekly_split_matches_unsplit_query_and_keeps_historical_nonflaky_membership(sqlite_engine, stored_key):
    with sqlite_engine.begin() as connection:
        _insert_issue(connection, 1, "TestHistory", "2026-10-01")
        connection.execute(text("""
            INSERT INTO ci_l1_builds
              (repo_full_name, target_branch, job_name, state, start_time, normalized_build_url, cloud_phase)
            VALUES
              ('pingcap/tidb', 'master', 'unit', 'success', '2026-10-01 10:00:00', 'https://prow.tidb.net/jenkins/job/unit/1/', 'GCP'),
              ('pingcap/tidb', 'master', 'unit', 'success', '2026-11-18 10:00:00', 'https://prow.tidb.net/jenkins/job/unit/2/', 'GCP')
        """))
        connection.execute(text("""
            INSERT INTO problem_case_runs
              (repo, branch, case_name, build_url, normalized_build_key, cloud_phase, report_time, flaky)
            VALUES ('pingcap/tidb', 'master', 'TestHistory', '/job/unit/1', :key, 'GCP', '2026-10-01 11:00:00', 0)
        """), {"key": stored_key})
    actual = flaky.get_issue_filtered_weekly_case_rates(sqlite_engine, FILTERS)
    assert actual["rows"][0]["cells"] == ["0.00% (0/1)"]

    # Reconstruct the pre-change single SELECT from the fallback arm. This oracle
    # exercises the same downstream joins while removing only the split predicate.
    transformed = []

    def unsplit(_connection, _cursor, statement, parameters, context, executemany):
        if "case_runs_raw AS (" in statement:
            start = statement.index("case_runs_raw AS (") + len("case_runs_raw AS (")
            end = statement.index("),\n            case_runs AS", start)
            arms = statement[start:end].split("UNION ALL")
            assert len(arms) == 2
            original = arms[1].replace("AND NULLIF(pcr.normalized_build_key, '') IS NULL", "")
            offset = statement[:start].count("?")
            parameters = parameters[:offset] + parameters[offset + arms[0].count("?"):]
            statement = statement[:start] + original + statement[end:]
            transformed.append(True)
        return statement, parameters

    event.listen(sqlite_engine, "before_cursor_execute", unsplit, retval=True)
    try:
        expected = flaky.get_issue_filtered_weekly_case_rates(sqlite_engine, FILTERS)
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", unsplit)
    assert transformed == [True]
    assert actual == expected
