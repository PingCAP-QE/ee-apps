from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GCSObjectRef:
    bucket: str
    object_name: str

    @property
    def uri(self) -> str:
        return f"gcs://{self.bucket}/{self.object_name}"


class GCSUploader:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client or _build_storage_client()

    def upload_text(
        self,
        *,
        bucket: str,
        object_name: str,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> str:
        bucket_obj = self._client.bucket(bucket)
        blob = bucket_obj.blob(object_name)
        blob.upload_from_string(text, content_type=content_type)
        return GCSObjectRef(bucket=bucket, object_name=object_name).uri


class GCSReader:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client or _build_storage_client()

    def download_text(
        self,
        *,
        bucket: str,
        object_name: str,
        encoding: str = "utf-8",
    ) -> str:
        bucket_obj = self._client.bucket(bucket)
        blob = bucket_obj.blob(object_name)
        return blob.download_as_text(encoding=encoding)


def parse_gcs_uri(uri: str | None) -> GCSObjectRef | None:
    if not uri or not uri.startswith("gcs://"):
        return None
    raw = uri[len("gcs://") :]
    if "/" not in raw:
        return None
    bucket, object_name = raw.split("/", 1)
    if not bucket or not object_name:
        return None
    return GCSObjectRef(bucket=bucket, object_name=object_name)


def _build_storage_client() -> Any:
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-storage is required for ci-dashboard GCS log operations; reinstall ci-dashboard dependencies"
        ) from exc
    return storage.Client()
