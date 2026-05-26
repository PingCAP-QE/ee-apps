from __future__ import annotations

from datetime import datetime
from datetime import timezone

from ci_dashboard.jobs.flaky import BuildAttempt, classify_state, compute_group_flags, has_retest_between, parse_datetime


def test_classify_state_keeps_abort_out_of_flaky_logic() -> None:
    assert classify_state("success") == "pass"
    assert classify_state("failure") == "fail"
    assert classify_state("aborted") == "skip"
    assert classify_state("canceled") == "ignore"


def test_compute_group_flags_marks_retry_loop_only_on_redundant_reruns() -> None:
    flags = compute_group_flags(
        [
            BuildAttempt(
                build_id=1,
                sha="sha-1",
                state="failure",
                created_at=datetime(2026, 4, 13, 10, 0, 0),
            ),
            BuildAttempt(
                build_id=2,
                sha="sha-1",
                state="failure",
                created_at=datetime(2026, 4, 13, 10, 5, 0),
            ),
            BuildAttempt(
                build_id=3,
                sha="sha-2",
                state="failure",
                created_at=datetime(2026, 4, 13, 10, 10, 0),
            ),
        ]
    )

    assert flags[1].is_retry_loop == 0
    assert flags[2].is_retry_loop == 1
    assert flags[3].is_retry_loop == 0


def test_parse_datetime_supports_datetime_strings_and_aware_values() -> None:
    aware = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)
    assert parse_datetime(None) is None
    assert parse_datetime("   ") is None
    assert parse_datetime(aware) == datetime(2026, 4, 13, 10, 0, 0)
    assert parse_datetime("2026-04-13T10:00:00Z") == datetime(2026, 4, 13, 10, 0, 0)
    assert parse_datetime(123) is None


def test_has_retest_between_checks_window_boundaries() -> None:
    retest_times = [
        datetime(2026, 4, 13, 10, 0, 0),
        datetime(2026, 4, 13, 10, 30, 0),
    ]
    assert has_retest_between(retest_times, datetime(2026, 4, 13, 9, 0, 0), datetime(2026, 4, 13, 10, 15, 0)) is True
    assert has_retest_between(retest_times, datetime(2026, 4, 13, 10, 31, 0), datetime(2026, 4, 13, 10, 40, 0)) is False
    assert has_retest_between([], datetime(2026, 4, 13, 9, 0, 0), datetime(2026, 4, 13, 10, 15, 0)) is False


def test_compute_group_flags_marks_flaky_when_failure_is_followed_by_pass() -> None:
    flags = compute_group_flags(
        [
            BuildAttempt(build_id=1, sha="sha-1", state="failure", created_at=datetime(2026, 4, 13, 10, 0, 0)),
            BuildAttempt(build_id=2, sha="sha-1", state="success", created_at=datetime(2026, 4, 13, 10, 1, 0)),
            BuildAttempt(build_id=3, sha="sha-1", state="pending", created_at=datetime(2026, 4, 13, 10, 2, 0)),
        ]
    )

    assert flags[1].is_flaky == 1
    assert flags[2].is_flaky == 0
    assert flags[3].is_flaky == 0


def test_compute_group_flags_requires_retest_signal_before_marking_retry_loop() -> None:
    attempts = [
        BuildAttempt(build_id=11, sha="sha-1", state="failure", created_at=datetime(2026, 4, 13, 10, 0, 0)),
        BuildAttempt(build_id=12, sha="sha-1", state="failure", created_at=datetime(2026, 4, 13, 10, 5, 0)),
        BuildAttempt(build_id=13, sha="sha-2", state="failure", created_at=datetime(2026, 4, 13, 10, 10, 0)),
    ]

    without_retest = compute_group_flags(attempts, require_retest=True, retest_times=[])
    with_retest = compute_group_flags(
        attempts,
        require_retest=True,
        retest_times=[datetime(2026, 4, 13, 10, 3, 0)],
    )

    assert without_retest[12].is_retry_loop == 0
    assert with_retest[12].is_retry_loop == 1


def test_compute_group_flags_returns_empty_for_no_attempts_and_ignores_unknown_state() -> None:
    assert compute_group_flags([]) == {}
    assert classify_state("mystery-state") == "ignore"
