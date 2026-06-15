from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class StorageBatchOperationsJob:
    job_name: str
    operation_name: str | None


@dataclass(frozen=True)
class StorageBatchOperationsJobStatus:
    job_name: str
    state: str
    total_object_count: int
    succeeded_object_count: int
    failed_object_count: int
    total_bytes_transformed: int
    complete_time: datetime | None


def create_delete_job(
    *,
    project_id: str,
    job_id: str,
    bucket_name: str,
    manifest_uri: str,
    dry_run: bool,
    description: str | None = None,
) -> StorageBatchOperationsJob:
    from google.auth import default
    from google.auth.transport.requests import Request as GoogleAuthRequest

    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not credentials.valid:
        credentials.refresh(GoogleAuthRequest())

    request_url = (
        "https://storagebatchoperations.googleapis.com/v1/"
        f"projects/{project_id}/locations/global/jobs?{urlencode({'jobId': job_id})}"
    )
    body = {
        "bucketList": {
            "buckets": [
                {
                    "bucket": bucket_name,
                    "manifest": {
                        "manifestLocation": manifest_uri,
                    },
                }
            ]
        },
        "deleteObject": {
            "permanentObjectDeletionEnabled": True,
        },
        "dryRun": dry_run,
        "loggingConfig": {
            "logActions": ["TRANSFORM"],
            "logActionStates": ["SUCCEEDED", "FAILED"],
        },
    }
    if description:
        body["description"] = description

    request = Request(
        request_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Storage Batch Operations create job failed ({exc.code}) for {job_id}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Storage Batch Operations create job failed for {job_id}: {str(exc.reason)}"
        ) from exc

    return StorageBatchOperationsJob(
        job_name=f"projects/{project_id}/locations/global/jobs/{job_id}",
        operation_name=payload.get("name"),
    )


def wait_for_delete_job(
    *,
    job_name: str,
    timeout_seconds: int = 7200,
    poll_interval_seconds: int = 10,
) -> StorageBatchOperationsJobStatus:
    from google.auth import default
    from google.auth.transport.requests import Request as GoogleAuthRequest

    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not credentials.valid:
        credentials.refresh(GoogleAuthRequest())

    request_url = f"https://storagebatchoperations.googleapis.com/v1/{job_name}"
    deadline = timeout_seconds
    elapsed = 0
    while True:
        payload = _get_job_payload(request_url=request_url, token=credentials.token)
        state = str(payload.get("state") or "STATE_UNSPECIFIED")
        if state not in {"RUNNING", "QUEUED", "STATE_UNSPECIFIED"}:
            return _coerce_job_status(job_name=job_name, payload=payload)
        if elapsed >= timeout_seconds:
            raise RuntimeError(
                f"Storage Batch Operations job timed out after {timeout_seconds}s: {job_name}"
            )
        sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds
        deadline -= poll_interval_seconds


def _get_job_payload(*, request_url: str, token: str) -> dict[str, object]:
    request = Request(
        request_url,
        headers={
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Storage Batch Operations get job failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"Storage Batch Operations get job failed: {str(exc.reason)}"
        ) from exc


def _coerce_job_status(
    *,
    job_name: str,
    payload: dict[str, object],
) -> StorageBatchOperationsJobStatus:
    counters = payload.get("counters") or {}
    if not isinstance(counters, dict):
        counters = {}
    return StorageBatchOperationsJobStatus(
        job_name=job_name,
        state=str(payload.get("state") or "STATE_UNSPECIFIED"),
        total_object_count=_coerce_counter(counters.get("totalObjectCount")),
        succeeded_object_count=_coerce_counter(counters.get("succeededObjectCount")),
        failed_object_count=_coerce_counter(counters.get("failedObjectCount")),
        total_bytes_transformed=_coerce_counter(counters.get("totalBytesTransformed")),
        complete_time=_coerce_optional_datetime(payload.get("completeTime")),
    )


def _coerce_counter(value: object) -> int:
    if value is None:
        return 0
    return int(str(value))


def _coerce_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Unsupported datetime value: {value!r}")
