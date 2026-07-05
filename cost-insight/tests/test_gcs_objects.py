from cost_insight.common import gcs_objects


class _FakeBlob:
    def __init__(self, generation: str | None, size: int | None = 1024) -> None:
        self.generation = generation
        self.size = size


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


def test_fetch_object_metadata_batch_returns_empty_tuple_for_empty_input(monkeypatch) -> None:
    monkeypatch.setattr(
        gcs_objects,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: _FakeClient(project=project_id, bucket=_FakeBucket({})),
    )

    assert gcs_objects.fetch_object_metadata_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        object_names=(),
    ) == ()


def test_fetch_object_metadata_batch_reads_objects_sequentially(monkeypatch) -> None:
    bucket = _FakeBucket(
        {
            "cas/present": _FakeBlob("123"),
            "cas/no-generation": _FakeBlob(None),
        }
    )
    monkeypatch.setattr(
        gcs_objects,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: _FakeClient(project=project_id, bucket=bucket),
    )

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
            size_bytes=1024,
        ),
        gcs_objects.GcsObjectMetadata(
            object_name="cas/missing",
            exists=False,
            generation=None,
            size_bytes=None,
        ),
        gcs_objects.GcsObjectMetadata(
            object_name="cas/no-generation",
            exists=True,
            generation=None,
            size_bytes=1024,
        ),
    )
    assert bucket.requested_names == ["cas/present", "cas/missing", "cas/no-generation"]


def test_fetch_object_metadata_batch_pool_maxsize_overrides_worker_count(monkeypatch) -> None:
    bucket = _FakeBucket(
        {
            "cas/one": _FakeBlob("11"),
            "cas/two": _FakeBlob("22"),
        }
    )
    observed_pool_sizes: list[int] = []
    monkeypatch.setattr(
        gcs_objects,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: (
            observed_pool_sizes.append(pool_maxsize)
            or _FakeClient(project=project_id, bucket=bucket)
        ),
    )
    used_max_workers: list[int] = []

    class _FakeExecutor:
        def __init__(self, *, max_workers: int) -> None:
            used_max_workers.append(max_workers)

        def __enter__(self):
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
        pool_maxsize=16,
    )

    assert used_max_workers == [4]
    assert observed_pool_sizes == [16]
    assert metadata == (
        gcs_objects.GcsObjectMetadata(object_name="cas/one", exists=True, generation=11, size_bytes=1024),
        gcs_objects.GcsObjectMetadata(object_name="cas/two", exists=True, generation=22, size_bytes=1024),
    )


def test_fetch_object_metadata_batch_preserves_explicit_zero_pool_maxsize(monkeypatch) -> None:
    observed_pool_sizes: list[int] = []
    monkeypatch.setattr(
        gcs_objects,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: (
            observed_pool_sizes.append(pool_maxsize)
            or _FakeClient(project=project_id, bucket=_FakeBucket({"cas/one": _FakeBlob("1")}))
        ),
    )

    gcs_objects.fetch_object_metadata_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        object_names=("cas/one",),
        max_workers=4,
        pool_maxsize=0,
    )

    assert observed_pool_sizes == [0]


def test_fetch_object_metadata_batch_floors_positive_pool_maxsize_at_worker_count(
    monkeypatch,
) -> None:
    observed_pool_sizes: list[int] = []
    monkeypatch.setattr(
        gcs_objects,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: (
            observed_pool_sizes.append(pool_maxsize)
            or _FakeClient(project=project_id, bucket=_FakeBucket({"cas/one": _FakeBlob("1")}))
        ),
    )

    gcs_objects.fetch_object_metadata_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        object_names=("cas/one",),
        max_workers=4,
        pool_maxsize=2,
    )

    assert observed_pool_sizes == [4]
