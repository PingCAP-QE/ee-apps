from __future__ import annotations

import sys
from types import ModuleType

import pytest

from cost_insight.common import gcs_objects


class _FakeBlob:
    def __init__(self, generation: str | None) -> None:
        self.generation = generation


class _FakeBucket:
    def __init__(self, blobs: dict[str, _FakeBlob | None]) -> None:
        self._blobs = blobs
        self.requested_names: list[str] = []

    def get_blob(self, object_name: str) -> _FakeBlob | None:
        self.requested_names.append(object_name)
        return self._blobs.get(object_name)


class _FakeClient:
    def __init__(self, *, project: str, bucket: _FakeBucket) -> None:
        self.project = project
        self._bucket = bucket

    def bucket(self, bucket_name: str) -> _FakeBucket:
        assert bucket_name == "test-bucket"
        return self._bucket


def _install_fake_storage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bucket: _FakeBucket,
) -> None:
    cloud_module = ModuleType("google.cloud")
    storage_module = ModuleType("google.cloud.storage")
    storage_module.Client = lambda project=None: _FakeClient(project=project, bucket=bucket)
    cloud_module.storage = storage_module
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_module)


def test_fetch_object_metadata_batch_returns_empty_tuple_for_empty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_storage(monkeypatch, bucket=_FakeBucket({}))

    assert gcs_objects.fetch_object_metadata_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        object_names=(),
    ) == ()


def test_fetch_object_metadata_batch_reads_objects_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bucket = _FakeBucket(
        {
            "cas/present": _FakeBlob("123"),
            "cas/no-generation": _FakeBlob(None),
        }
    )
    _install_fake_storage(monkeypatch, bucket=bucket)

    metadata = gcs_objects.fetch_object_metadata_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        object_names=("cas/present", "cas/missing", "cas/no-generation"),
        max_workers=1,
    )

    assert metadata == (
        gcs_objects.GcsObjectMetadata(
            object_name="cas/present",
            exists=True,
            generation=123,
        ),
        gcs_objects.GcsObjectMetadata(
            object_name="cas/missing",
            exists=False,
            generation=None,
        ),
        gcs_objects.GcsObjectMetadata(
            object_name="cas/no-generation",
            exists=True,
            generation=None,
        ),
    )
    assert bucket.requested_names == ["cas/present", "cas/missing", "cas/no-generation"]


def test_fetch_object_metadata_batch_uses_executor_for_parallel_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bucket = _FakeBucket(
        {
            "cas/one": _FakeBlob("11"),
            "cas/two": _FakeBlob("22"),
        }
    )
    _install_fake_storage(monkeypatch, bucket=bucket)
    used_max_workers: list[int] = []

    class _FakeExecutor:
        def __init__(self, *, max_workers: int) -> None:
            used_max_workers.append(max_workers)

        def __enter__(self) -> _FakeExecutor:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def map(self, fn, values):
            return [fn(value) for value in values]

    monkeypatch.setattr(gcs_objects, "ThreadPoolExecutor", _FakeExecutor)

    metadata = gcs_objects.fetch_object_metadata_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        object_names=("cas/one", "cas/two"),
        max_workers=4,
    )

    assert used_max_workers == [4]
    assert metadata == (
        gcs_objects.GcsObjectMetadata(object_name="cas/one", exists=True, generation=11),
        gcs_objects.GcsObjectMetadata(object_name="cas/two", exists=True, generation=22),
    )
