from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence


PASS_STATES = {"success", "pass"}
FAIL_STATES = {"failure", "error", "timeout", "timed_out"}
IGNORE_STATES = {"canceled", "cancelled", "pending", "triggered"}


@dataclass(frozen=True)
class BuildAttempt:
    build_id: int
    sha: str
    state: str | None
    created_at: datetime | None


@dataclass
class Flags:
    is_flaky: int = 0
    is_retry_loop: int = 0


def parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_naive_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        return _to_naive_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    return None


def classify_state(
    state: str | None,
    pass_states: set[str] | None = None,
    fail_states: set[str] | None = None,
    ignore_states: set[str] | None = None,
) -> str:
    if state is None:
        return "ignore"
    normalized = state.strip().lower()
    if normalized == "aborted":
        return "skip"

    effective_pass_states = pass_states or PASS_STATES
    effective_fail_states = fail_states or FAIL_STATES
    effective_ignore_states = ignore_states or IGNORE_STATES

    if normalized in effective_pass_states:
        return "pass"
    if normalized in effective_fail_states:
        return "fail"
    if normalized in effective_ignore_states:
        return "ignore"
    return "ignore"


def has_retest_between(
    retest_times: Sequence[datetime],
    start: datetime,
    end: datetime,
) -> bool:
    if not retest_times:
        return False
    index = bisect.bisect_left(retest_times, start)
    if index >= len(retest_times):
        return False
    return retest_times[index] < end


def compute_group_flags(
    attempts: Sequence[BuildAttempt],
    *,
    require_retest: bool = False,
    retest_times: Sequence[datetime] = (),
) -> dict[int, Flags]:
    if not attempts:
        return {}

    ordered = sorted(
        attempts,
        key=lambda item: (
            item.created_at or datetime.min,
            item.build_id,
        ),
    )
    flags = {attempt.build_id: Flags() for attempt in ordered}

    current_sha: str | None = None
    segment_failures: list[BuildAttempt] = []
    failure_streak: list[BuildAttempt] = []
    segment_attempt_count = 0
    segment_has_pass = False
    first_failure_time: datetime | None = None

    def finalize_segment(next_sha_time: datetime | None) -> None:
        nonlocal segment_failures, segment_has_pass, first_failure_time, segment_attempt_count
        if segment_has_pass:
            return
        if next_sha_time is None:
            return
        if segment_attempt_count < 2:
            return
        if require_retest:
            if first_failure_time is None:
                return
            if not has_retest_between(retest_times, first_failure_time, next_sha_time):
                return
        for attempt in segment_failures:
            if flags[attempt.build_id].is_flaky == 0:
                flags[attempt.build_id].is_retry_loop = 1

    for attempt in ordered:
        if current_sha is None:
            current_sha = attempt.sha

        if attempt.sha != current_sha:
            finalize_segment(attempt.created_at)
            current_sha = attempt.sha
            segment_failures = []
            failure_streak = []
            segment_attempt_count = 0
            segment_has_pass = False
            first_failure_time = None

        state_class = classify_state(attempt.state)
        if state_class == "skip":
            continue

        segment_attempt_count += 1
        attempt_index = segment_attempt_count

        if state_class == "fail":
            failure_streak.append(attempt)
            if attempt_index >= 2:
                segment_failures.append(attempt)
            if first_failure_time is None and attempt.created_at is not None:
                first_failure_time = attempt.created_at
            continue

        if state_class == "pass":
            if failure_streak:
                for failed in failure_streak:
                    flags[failed.build_id].is_flaky = 1
                failure_streak = []
            segment_has_pass = True
            continue

        failure_streak = []

    finalize_segment(None)
    return flags


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
