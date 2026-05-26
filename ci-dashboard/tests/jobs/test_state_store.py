from __future__ import annotations

from ci_dashboard.jobs.state_store import (
    _parse_watermark,
    get_job_state,
    mark_job_failed,
    mark_job_started,
    mark_job_succeeded,
    save_job_progress,
)


def test_parse_watermark_handles_dict_and_empty_string() -> None:
    assert _parse_watermark({"a": 1}) == {"a": 1}
    assert _parse_watermark("") == {}


def test_state_store_lifecycle_with_sqlite(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        mark_job_started(connection, "job-a", {"last_id": 1})
        save_job_progress(connection, "job-a", {"last_id": 2})
        mark_job_succeeded(connection, "job-a", {"last_id": 3})

    with sqlite_engine.begin() as connection:
        state = get_job_state(connection, "job-a")

    assert state is not None
    assert state.job_name == "job-a"
    assert state.watermark == {"last_id": 3}
    assert state.last_status == "succeeded"
    assert state.last_started_at is not None
    assert state.last_succeeded_at is not None


def test_state_store_records_failure(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        mark_job_failed(connection, "job-b", {"last_id": 4}, "boom")

    with sqlite_engine.begin() as connection:
        state = get_job_state(connection, "job-b")

    assert state is not None
    assert state.last_status == "failed"
    assert state.last_error == "boom"
