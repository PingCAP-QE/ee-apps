from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError

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


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_create_delete_job_posts_expected_request_and_returns_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_google_auth(monkeypatch)
    seen_request: dict[str, object] = {}

    def fake_urlopen(request, timeout: int):
        seen_request["url"] = request.full_url
        seen_request["method"] = request.get_method()
        seen_request["authorization"] = request.headers["Authorization"]
        seen_request["content_type"] = request.headers["Content-type"]
        seen_request["body"] = json.loads(request.data.decode("utf-8"))
        assert timeout == 120
        return _FakeHttpResponse({"name": "operations/delete-job-1"})

    monkeypatch.setattr(storage_batch_operations, "urlopen", fake_urlopen)

    job = storage_batch_operations.create_delete_job(
        project_id="test-project",
        job_id="delete-job-1",
        bucket_name="test-bucket",
        manifest_uri="gs://manifest-bucket/path/manifest.csv",
        dry_run=False,
        description="steady-state cleanup",
    )

    assert job == storage_batch_operations.StorageBatchOperationsJob(
        job_name="projects/test-project/locations/global/jobs/delete-job-1",
        operation_name="operations/delete-job-1",
    )
    assert seen_request == {
        "url": (
            "https://storagebatchoperations.googleapis.com/v1/projects/test-project/"
            "locations/global/jobs?jobId=delete-job-1"
        ),
        "method": "POST",
        "authorization": "Bearer test-token",
        "content_type": "application/json",
        "body": {
            "bucketList": {
                "buckets": [
                    {
                        "bucket": "test-bucket",
                        "manifest": {
                            "manifestLocation": "gs://manifest-bucket/path/manifest.csv",
                        },
                    }
                ]
            },
            "deleteObject": {"permanentObjectDeletionEnabled": True},
            "dryRun": False,
            "loggingConfig": {
                "logActions": ["TRANSFORM"],
                "logActionStates": ["SUCCEEDED", "FAILED"],
            },
            "description": "steady-state cleanup",
        },
    }


def test_create_delete_job_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_google_auth(monkeypatch)

    def fake_urlopen(_request, timeout: int):
        assert timeout == 120
        raise HTTPError(
            url="https://example.invalid/job",
            code=400,
            msg="bad request",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"boom"}'),
        )

    monkeypatch.setattr(storage_batch_operations, "urlopen", fake_urlopen)

    with pytest.raises(
        RuntimeError,
        match='Storage Batch Operations create job failed \\(400\\) for delete-job-2: \\{"error":"boom"\\}',
    ):
        storage_batch_operations.create_delete_job(
            project_id="test-project",
            job_id="delete-job-2",
            bucket_name="test-bucket",
            manifest_uri="gs://manifest-bucket/path/manifest.csv",
            dry_run=True,
        )


def test_create_delete_job_wraps_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_google_auth(monkeypatch)
    monkeypatch.setattr(
        storage_batch_operations,
        "urlopen",
        lambda _request, timeout: (_ for _ in ()).throw(URLError("network down")),
    )

    with pytest.raises(
        RuntimeError,
        match="Storage Batch Operations create job failed for delete-job-3: network down",
    ):
        storage_batch_operations.create_delete_job(
            project_id="test-project",
            job_id="delete-job-3",
            bucket_name="test-bucket",
            manifest_uri="gs://manifest-bucket/path/manifest.csv",
            dry_run=True,
        )


def test_get_job_payload_handles_success_and_error_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            _FakeHttpResponse({"state": "RUNNING"}),
            HTTPError(
                url="https://example.invalid/job",
                code=503,
                msg="unavailable",
                hdrs=None,
                fp=io.BytesIO(b"temporary outage"),
            ),
            HTTPError(
                url="https://example.invalid/job",
                code=404,
                msg="missing",
                hdrs=None,
                fp=io.BytesIO(b"missing"),
            ),
            URLError("socket timeout"),
        ]
    )

    def fake_urlopen(_request, timeout: int):
        assert timeout == 120
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(storage_batch_operations, "urlopen", fake_urlopen)

    assert storage_batch_operations._get_job_payload(
        request_url="https://example.invalid/job",
        token="test-token",
    ) == {"state": "RUNNING"}

    with pytest.raises(
        storage_batch_operations.StorageBatchOperationsTransientError,
        match="Storage Batch Operations get job transient failure \\(503\\): temporary outage",
    ):
        storage_batch_operations._get_job_payload(
            request_url="https://example.invalid/job",
            token="test-token",
        )

    with pytest.raises(
        RuntimeError,
        match="Storage Batch Operations get job failed \\(404\\): missing",
    ):
        storage_batch_operations._get_job_payload(
            request_url="https://example.invalid/job",
            token="test-token",
        )

    with pytest.raises(
        storage_batch_operations.StorageBatchOperationsTransientError,
        match="Storage Batch Operations get job transient failure: socket timeout",
    ):
        storage_batch_operations._get_job_payload(
            request_url="https://example.invalid/job",
            token="test-token",
        )


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


def test_wait_for_delete_job_returns_terminal_failure_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_google_auth(monkeypatch)

    monkeypatch.setattr(
        storage_batch_operations,
        "_get_job_payload",
        lambda **_: {"state": "FAILED"},
    )

    status = storage_batch_operations.wait_for_delete_job(
        job_name="projects/test/locations/global/jobs/job-3",
        timeout_seconds=20,
        poll_interval_seconds=10,
    )

    assert status == storage_batch_operations.StorageBatchOperationsJobStatus(
        job_name="projects/test/locations/global/jobs/job-3",
        state="FAILED",
        total_object_count=0,
        succeeded_object_count=0,
        failed_object_count=0,
        total_bytes_transformed=0,
        complete_time=None,
    )
