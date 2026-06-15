from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class StorageBatchOperationsJob:
    job_name: str
    operation_name: str | None


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
