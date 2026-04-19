from __future__ import annotations

from datetime import datetime

from ci_dashboard.jobs.flaky import BuildAttempt, classify_state, compute_group_flags


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
