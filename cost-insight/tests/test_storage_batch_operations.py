from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cost_insight.common import storage_batch_operations


class _FakeCredentials:
    def __init__(self) -> None:
        self.valid = True
        self.token = "test-token"

    def refresh(self, _request) -> None:
        self.valid = True


def _install_fake_google_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    import google.auth
    from google.auth.transport import requests

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_FakeCredentials(), "test"))
    monkeypatch.setattr(requests, "Request", lambda: object())


def test_wait_for_delete_job_retries_transient_errors_and_state_unspecified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_google_auth(monkeypatch)

    responses = iter(
        [
            storage_batch_operations.StorageBatchOperationsTransientError("retry later"),
            {"state": "STATE_UNSPECIFIED"},
            {"state": "RUNNING"},
            {
                "state": "SUCCEEDED",
                "counters": {
                    "totalObjectCount": "7",
                    "succeededObjectCount": "7",
                    "failedObjectCount": "0",
                    "totalBytesTransformed": "123",
                },
                "completeTime": "2026-06-15T08:01:00Z",
            },
        ]
    )
    sleeps: list[int] = []

    def fake_get_job_payload(*, request_url: str, token: str) -> dict[str, object]:
        assert request_url.endswith("/projects/test/locations/global/jobs/job-1")
        assert token == "test-token"
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(storage_batch_operations, "_get_job_payload", fake_get_job_payload)
    monkeypatch.setattr(storage_batch_operations, "sleep", lambda seconds: sleeps.append(seconds))

    status = storage_batch_operations.wait_for_delete_job(
        job_name="projects/test/locations/global/jobs/job-1",
        timeout_seconds=60,
        poll_interval_seconds=5,
    )

    assert status == storage_batch_operations.StorageBatchOperationsJobStatus(
        job_name="projects/test/locations/global/jobs/job-1",
        state="SUCCEEDED",
        total_object_count=7,
        succeeded_object_count=7,
        failed_object_count=0,
        total_bytes_transformed=123,
        complete_time=datetime(2026, 6, 15, 8, 1, tzinfo=UTC),
    )
    assert sleeps == [5, 5, 5]


def test_wait_for_delete_job_times_out_for_non_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_google_auth(monkeypatch)

    monkeypatch.setattr(
        storage_batch_operations,
        "_get_job_payload",
        lambda **_: {"state": "RUNNING"},
    )
    sleeps: list[int] = []
    monkeypatch.setattr(storage_batch_operations, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(
        RuntimeError,
        match=(
            "Storage Batch Operations job timed out after 20s: "
            "projects/test/locations/global/jobs/job-2"
        ),
    ):
        storage_batch_operations.wait_for_delete_job(
            job_name="projects/test/locations/global/jobs/job-2",
            timeout_seconds=20,
            poll_interval_seconds=10,
        )

    assert sleeps == [10, 10]
