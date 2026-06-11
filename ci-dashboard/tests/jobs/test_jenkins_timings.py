from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.jenkins_timings import (
    fetch_and_store_jenkins_timings,
    parse_jenkins_duration_seconds,
    parse_jenkins_timings,
    run_backfill_jenkins_timings,
)

TIMINGS_HTML = """
<html>
  <body>
    <table class="jenkins-table">
      <thead>
        <tr><th></th><th></th><th>Primary task</th><th>Including subtasks</th></tr>
      </thead>
      <tbody>
        <tr><td rowspan="5">In queue</td></tr>
        <tr><td>Waiting</td><td>5 sec</td><td>5 sec</td></tr>
        <tr><td>Blocked</td><td>0 ms</td><td>1 min 30 sec</td></tr>
        <tr><td>Buildable</td><td>0 ms</td><td>2 hr 3 min</td></tr>
        <tr><td>Total</td><td>5 sec</td><td>7 hr 28 min</td></tr>
        <tr><td colspan="2">Building</td><td>1 hr 50 min</td><td>4 hr 0 min</td></tr>
        <tr><td colspan="2">Scheduled to completion</td><td colspan="2">1 hr 50 min</td></tr>
        <tr><td colspan="2">Number of subtasks</td><td colspan="2">12</td></tr>
        <tr><td colspan="2">Average executor utilization</td><td colspan="2">2.2</td></tr>
      </tbody>
    </table>
  </body>
</html>
"""


def _settings() -> Settings:
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
        jobs=JobSettings(batch_size=10),
        log_level="INFO",
    )


class _FakeFetcher:
    def __init__(self, html_text: str = TIMINGS_HTML) -> None:
        self.html_text = html_text
        self.calls: list[tuple[str, int]] = []

    def fetch_timings_html(self, build_url: str, *, timeout_seconds: int) -> str:
        self.calls.append((build_url, timeout_seconds))
        return self.html_text


def _insert_build(
    sqlite_engine,
    *,
    build_id: int,
    completion_time: datetime,
    build_system: str = "JENKINS",
    queue_total_sum: int | None = None,
) -> None:
    build_url = f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-a/{build_id}/"
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  id, state, optional, report, is_pr_build, url, normalized_build_url,
                  completion_time, cloud_phase, build_system, jenkins_queue_total_subtasks_sum
                ) VALUES (
                  :id, 'success', 0, 1, 0, :url, :url,
                  :completion_time, 'GCP', :build_system, :queue_total_sum
                )
                """
            ),
            {
                "id": build_id,
                "url": build_url,
                "completion_time": completion_time.strftime("%Y-%m-%d %H:%M:%S"),
                "build_system": build_system,
                "queue_total_sum": queue_total_sum,
            },
        )


def test_parse_jenkins_duration_seconds_supports_compound_values() -> None:
    assert parse_jenkins_duration_seconds("7 hr 28 min") == 26880
    assert parse_jenkins_duration_seconds("4 hr 0 min") == 14400
    assert parse_jenkins_duration_seconds("1 min 30 sec") == 90
    assert parse_jenkins_duration_seconds("0 ms") == 0
    assert parse_jenkins_duration_seconds("499 ms") == 0
    assert parse_jenkins_duration_seconds("500 ms") == 1
    assert parse_jenkins_duration_seconds("89.999999 sec") == 90


def test_parse_jenkins_timings_keeps_only_subtask_aggregates() -> None:
    timings = parse_jenkins_timings(TIMINGS_HTML)

    assert timings.blocked_subtasks_sum == 90
    assert timings.buildable_subtasks_sum == 7380
    assert timings.queue_total_subtasks_sum == 26880
    assert timings.building_subtasks_sum == 14400
    assert timings.subtask_count == 12


def test_fetch_and_store_jenkins_timings_updates_canonical_build(sqlite_engine) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _insert_build(sqlite_engine, build_id=101, completion_time=now)
    fetcher = _FakeFetcher()

    fetch_and_store_jenkins_timings(
        sqlite_engine,
        build_id=101,
        build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-a/101/",
        fetcher=fetcher,
    )

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT jenkins_blocked_subtasks_sum,
                       jenkins_buildable_subtasks_sum,
                       jenkins_queue_total_subtasks_sum,
                       jenkins_building_subtasks_sum,
                       jenkins_subtask_count
                FROM ci_l1_builds
                WHERE id = 101
                """
            )
        ).mappings().one()

    assert dict(row) == {
        "jenkins_blocked_subtasks_sum": 90,
        "jenkins_buildable_subtasks_sum": 7380,
        "jenkins_queue_total_subtasks_sum": 26880,
        "jenkins_building_subtasks_sum": 14400,
        "jenkins_subtask_count": 12,
    }
    assert fetcher.calls == [
        (
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-a/101/",
            5,
        )
    ]


def test_backfill_jenkins_timings_scans_recent_missing_jenkins_rows(sqlite_engine) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _insert_build(sqlite_engine, build_id=201, completion_time=now - timedelta(days=1))
    _insert_build(sqlite_engine, build_id=202, completion_time=now - timedelta(days=31))
    _insert_build(
        sqlite_engine,
        build_id=203,
        completion_time=now - timedelta(days=1),
        build_system="PROW_NATIVE",
    )
    _insert_build(
        sqlite_engine,
        build_id=204,
        completion_time=now - timedelta(days=1),
        queue_total_sum=10,
    )
    fetcher = _FakeFetcher()

    summary = run_backfill_jenkins_timings(
        sqlite_engine,
        _settings(),
        lookback_days=30,
        fetcher=fetcher,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_updated == 1
    assert summary.builds_failed == 0
    assert fetcher.calls[0][0].endswith("/201/")
