from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

from ci_dashboard.jobs.gcs_client import (
    GCSObjectRef,
    GCSReader,
    GCSUploader,
    _build_storage_client,
    parse_gcs_uri,
)


class _FakeBlob:
    def __init__(self) -> None:
        self.upload_calls: list[tuple[str, str]] = []
        self.download_result = "downloaded-text"

    def upload_from_string(self, text: str, *, content_type: str) -> None:
        self.upload_calls.append((text, content_type))

    def download_as_text(self, *, encoding: str) -> str:
        return f"{self.download_result}:{encoding}"


class _FakeBucket:
    def __init__(self, blob: _FakeBlob) -> None:
        self._blob = blob
        self.requested_names: list[str] = []

    def blob(self, object_name: str) -> _FakeBlob:
        self.requested_names.append(object_name)
        return self._blob


class _FakeClient:
    def __init__(self, bucket: _FakeBucket) -> None:
        self._bucket = bucket
        self.requested_buckets: list[str] = []

    def bucket(self, bucket_name: str) -> _FakeBucket:
        self.requested_buckets.append(bucket_name)
        return self._bucket


def test_gcs_object_ref_builds_uri() -> None:
    assert GCSObjectRef(bucket="ci-logs", object_name="202604/build-1.log").uri == "gcs://ci-logs/202604/build-1.log"


def test_gcs_uploader_and_reader_delegate_to_storage_client() -> None:
    blob = _FakeBlob()
    bucket = _FakeBucket(blob)
    client = _FakeClient(bucket)

    uploader = GCSUploader(client=client)
    reader = GCSReader(client=client)

    uri = uploader.upload_text(
        bucket="ci-logs",
        object_name="202604/build-1.log",
        text="hello world",
        content_type="text/plain",
    )
    content = reader.download_text(
        bucket="ci-logs",
        object_name="202604/build-1.log",
        encoding="utf-16",
    )

    assert uri == "gcs://ci-logs/202604/build-1.log"
    assert client.requested_buckets == ["ci-logs", "ci-logs"]
    assert bucket.requested_names == ["202604/build-1.log", "202604/build-1.log"]
    assert blob.upload_calls == [("hello world", "text/plain")]
    assert content == "downloaded-text:utf-16"


def test_parse_gcs_uri_accepts_valid_uri_and_rejects_invalid_values() -> None:
    assert parse_gcs_uri("gcs://ci-logs/202604/build-1.log") == GCSObjectRef(
        bucket="ci-logs",
        object_name="202604/build-1.log",
    )
    assert parse_gcs_uri(None) is None
    assert parse_gcs_uri("") is None
    assert parse_gcs_uri("https://storage.googleapis.com/ci-logs/build.log") is None
    assert parse_gcs_uri("gcs://ci-logs") is None
    assert parse_gcs_uri("gcs:///missing-bucket") is None
    assert parse_gcs_uri("gcs://ci-logs/") is None


def test_build_storage_client_raises_clear_error_when_dependency_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google.cloud":
            raise ImportError("missing google.cloud")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="google-cloud-storage is required"):
        _build_storage_client()


def test_build_storage_client_returns_google_storage_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_storage = SimpleNamespace(Client=lambda: "fake-storage-client")
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google.cloud":
            return SimpleNamespace(storage=fake_storage)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert _build_storage_client() == "fake-storage-client"
