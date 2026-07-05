import pytest

from cost_insight.common import gcs_cache_references
from cost_insight.common.gcs_cache_references import (
    extract_cas_references_from_action_result_bytes,
    extract_cas_references_from_tree_bytes,
)


def test_extract_cas_references_from_action_result_bytes_includes_output_files_stdout_and_tree() -> (
    None
):
    tree_bytes = _tree(
        root=_directory(files=[_file_node("root.txt", "tree-root-file", 10)]),
        children=[_directory(files=[_file_node("child.txt", "tree-child-file", 11)])],
    )
    blobs = {"tree-digest": tree_bytes}
    action_result = _action_result(
        output_files=[_output_file("bazel-out/bin/file", "file-digest", 1)],
        stdout_digest=_digest("stdout-digest", 2),
        stderr_digest=_digest("stderr-digest", 3),
        output_directories=[
            _output_directory("bazel-out/bin/dir", tree_digest=_digest("tree-digest", 4))
        ],
    )

    references = extract_cas_references_from_action_result_bytes(
        action_result,
        fetch_cas_blob=lambda hash_value: blobs.get(hash_value),
    )

    assert references == {
        "cas/file-digest",
        "cas/stdout-digest",
        "cas/stderr-digest",
        "cas/tree-digest",
        "cas/tree-root-file",
        "cas/tree-child-file",
    }


def test_extract_cas_references_from_action_result_bytes_handles_directory_only_outputs() -> None:
    child_directory = _directory(files=[_file_node("child.txt", "child-file", 12)])
    root_directory = _directory(
        files=[_file_node("root.txt", "root-file", 13)],
        directories=[_directory_node("nested", "child-dir", 14)],
    )
    blobs = {
        "root-dir": root_directory,
        "child-dir": child_directory,
    }
    action_result = _action_result(
        output_directories=[
            _output_directory("bazel-out/bin/tree", root_directory_digest=_digest("root-dir", 15))
        ],
    )

    references = extract_cas_references_from_action_result_bytes(
        action_result,
        fetch_cas_blob=lambda hash_value: blobs.get(hash_value),
    )

    assert references == {
        "cas/root-dir",
        "cas/child-dir",
        "cas/root-file",
        "cas/child-file",
    }


def test_extract_cas_references_from_tree_bytes_reads_root_and_children() -> None:
    tree_bytes = _tree(
        root=_directory(files=[_file_node("root.txt", "root-file", 1)]),
        children=[_directory(files=[_file_node("child.txt", "child-file", 2)])],
    )

    assert extract_cas_references_from_tree_bytes(tree_bytes) == {
        "cas/root-file",
        "cas/child-file",
    }


def test_extract_cas_references_from_tree_bytes_rejects_truncated_length_delimited_field() -> None:
    tree_bytes = _varint((1 << 3) | 2) + _varint(10) + b"abc"

    with pytest.raises(ValueError, match="Truncated protobuf length-delimited field"):
        extract_cas_references_from_tree_bytes(tree_bytes)


def test_extract_cas_references_from_tree_bytes_rejects_overlong_varint() -> None:
    tree_bytes = b"\x80" * 11

    with pytest.raises(ValueError, match="Protobuf varint exceeds 10 bytes"):
        extract_cas_references_from_tree_bytes(tree_bytes)


def test_extract_action_cache_references_batch_pool_maxsize_overrides_worker_count(
    monkeypatch,
) -> None:
    observed_pool_sizes: list[int] = []

    class _FakeBucket:
        def blob(self, object_name: str):
            class _FakeBlob:
                def download_as_bytes(self) -> bytes:
                    return b""

            return _FakeBlob()

    class _FakeClient:
        def bucket(self, bucket_name: str) -> _FakeBucket:
            assert bucket_name == "test-bucket"
            return _FakeBucket()

    def _fake_create_storage_client(*, project_id: str, pool_maxsize: int):
        assert project_id == "test-project"
        observed_pool_sizes.append(pool_maxsize)
        return _FakeClient()

    monkeypatch.setattr(gcs_cache_references, "create_storage_client", _fake_create_storage_client)
    monkeypatch.setattr(
        gcs_cache_references,
        "extract_cas_references_from_action_result_bytes",
        lambda action_result_bytes, *, fetch_cas_blob: {"cas/a", "cas/b"},
    )

    rows = gcs_cache_references.extract_action_cache_references_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        ac_object_names=("ac/one", "ac/two"),
        max_workers=32,
        pool_maxsize=96,
    )

    assert observed_pool_sizes == [96]
    assert rows == (
        gcs_cache_references.AcReferenceExtraction(
            ac_object_name="ac/one",
            exists=True,
            cas_object_names=("cas/a", "cas/b"),
        ),
        gcs_cache_references.AcReferenceExtraction(
            ac_object_name="ac/two",
            exists=True,
            cas_object_names=("cas/a", "cas/b"),
        ),
    )


def test_extract_action_cache_references_batch_preserves_explicit_zero_pool_maxsize(
    monkeypatch,
) -> None:
    observed_pool_sizes: list[int] = []

    class _FakeBucket:
        def blob(self, object_name: str):
            class _FakeBlob:
                def download_as_bytes(self) -> bytes:
                    return b""

            return _FakeBlob()

    class _FakeClient:
        def bucket(self, bucket_name: str) -> _FakeBucket:
            assert bucket_name == "test-bucket"
            return _FakeBucket()

    monkeypatch.setattr(
        gcs_cache_references,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: (
            observed_pool_sizes.append(pool_maxsize) or _FakeClient()
        ),
    )
    monkeypatch.setattr(
        gcs_cache_references,
        "extract_cas_references_from_action_result_bytes",
        lambda action_result_bytes, *, fetch_cas_blob: set(),
    )

    gcs_cache_references.extract_action_cache_references_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        ac_object_names=("ac/one",),
        max_workers=32,
        pool_maxsize=0,
    )

    assert observed_pool_sizes == [0]


def test_extract_action_cache_references_batch_floors_positive_pool_maxsize_at_worker_count(
    monkeypatch,
) -> None:
    observed_pool_sizes: list[int] = []

    class _FakeBucket:
        def blob(self, object_name: str):
            class _FakeBlob:
                def download_as_bytes(self) -> bytes:
                    return b""

            return _FakeBlob()

    class _FakeClient:
        def bucket(self, bucket_name: str) -> _FakeBucket:
            assert bucket_name == "test-bucket"
            return _FakeBucket()

    monkeypatch.setattr(
        gcs_cache_references,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: (
            observed_pool_sizes.append(pool_maxsize) or _FakeClient()
        ),
    )
    monkeypatch.setattr(
        gcs_cache_references,
        "extract_cas_references_from_action_result_bytes",
        lambda action_result_bytes, *, fetch_cas_blob: set(),
    )

    gcs_cache_references.extract_action_cache_references_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        ac_object_names=("ac/one",),
        max_workers=32,
        pool_maxsize=16,
    )

    assert observed_pool_sizes == [32]


def test_extract_action_cache_references_batch_shares_cas_blob_cache(monkeypatch) -> None:
    tree_bytes = _tree(root=_directory(files=[_file_node("root.txt", "tree-root-file", 10)]))
    action_result = _action_result(
        output_directories=[
            _output_directory("bazel-out/bin/dir", tree_digest=_digest("tree-digest", 4))
        ],
    )
    download_counts: dict[str, int] = {}

    class _FakeBucket:
        def blob(self, object_name: str):
            class _FakeBlob:
                def download_as_bytes(self) -> bytes:
                    download_counts[object_name] = download_counts.get(object_name, 0) + 1
                    if object_name.startswith("ac/"):
                        return action_result
                    if object_name == "cas/tree-digest":
                        return tree_bytes
                    raise AssertionError(f"Unexpected object download: {object_name}")

            return _FakeBlob()

    class _FakeClient:
        def bucket(self, bucket_name: str) -> _FakeBucket:
            assert bucket_name == "test-bucket"
            return _FakeBucket()

    monkeypatch.setattr(
        gcs_cache_references,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: _FakeClient(),
    )

    rows = gcs_cache_references.extract_action_cache_references_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        ac_object_names=("ac/one", "ac/two"),
        max_workers=1,
    )

    assert rows == (
        gcs_cache_references.AcReferenceExtraction(
            ac_object_name="ac/one",
            exists=True,
            cas_object_names=("cas/tree-digest", "cas/tree-root-file"),
        ),
        gcs_cache_references.AcReferenceExtraction(
            ac_object_name="ac/two",
            exists=True,
            cas_object_names=("cas/tree-digest", "cas/tree-root-file"),
        ),
    )
    assert download_counts["cas/tree-digest"] == 1


def test_extract_action_cache_references_batch_caches_missing_cas_blobs(monkeypatch) -> None:
    from google.api_core.exceptions import NotFound

    action_result = _action_result(
        output_directories=[
            _output_directory("bazel-out/bin/dir", tree_digest=_digest("missing-tree", 4))
        ],
    )
    download_counts: dict[str, int] = {}

    class _FakeBucket:
        def blob(self, object_name: str):
            class _FakeBlob:
                def download_as_bytes(self) -> bytes:
                    download_counts[object_name] = download_counts.get(object_name, 0) + 1
                    if object_name.startswith("ac/"):
                        return action_result
                    if object_name == "cas/missing-tree":
                        raise NotFound("not found")
                    raise AssertionError(f"Unexpected object download: {object_name}")

            return _FakeBlob()

    class _FakeClient:
        def bucket(self, bucket_name: str) -> _FakeBucket:
            assert bucket_name == "test-bucket"
            return _FakeBucket()

    monkeypatch.setattr(
        gcs_cache_references,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: _FakeClient(),
    )

    rows = gcs_cache_references.extract_action_cache_references_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        ac_object_names=("ac/one", "ac/two"),
        max_workers=1,
    )

    assert rows == (
        gcs_cache_references.AcReferenceExtraction(
            ac_object_name="ac/one",
            exists=True,
            cas_object_names=("cas/missing-tree",),
        ),
        gcs_cache_references.AcReferenceExtraction(
            ac_object_name="ac/two",
            exists=True,
            cas_object_names=("cas/missing-tree",),
        ),
    )
    assert download_counts["cas/missing-tree"] == 1


def test_extract_action_cache_references_batch_returns_parse_error_for_corrupt_ac(
    monkeypatch,
) -> None:
    corrupt_action_result = _field_varint(1, 1)[:-1] + b"\x80" * 11

    class _FakeBucket:
        def blob(self, object_name: str):
            class _FakeBlob:
                def download_as_bytes(self) -> bytes:
                    assert object_name == "ac/corrupt"
                    return corrupt_action_result

            return _FakeBlob()

    class _FakeClient:
        def bucket(self, bucket_name: str) -> _FakeBucket:
            assert bucket_name == "test-bucket"
            return _FakeBucket()

    monkeypatch.setattr(
        gcs_cache_references,
        "create_storage_client",
        lambda *, project_id, pool_maxsize: _FakeClient(),
    )

    rows = gcs_cache_references.extract_action_cache_references_batch(
        project_id="test-project",
        bucket_name="test-bucket",
        ac_object_names=("ac/corrupt",),
        max_workers=1,
    )

    assert rows == (
        gcs_cache_references.AcReferenceExtraction(
            ac_object_name="ac/corrupt",
            exists=True,
            cas_object_names=(),
            parse_error="Protobuf varint exceeds 10 bytes",
        ),
    )


def _action_result(
    *, output_files=(), output_directories=(), stdout_digest=None, stderr_digest=None
) -> bytes:
    parts = []
    for output_file in output_files:
        parts.append(_field_bytes(2, output_file))
    for output_directory in output_directories:
        parts.append(_field_bytes(3, output_directory))
    if stdout_digest is not None:
        parts.append(_field_bytes(7, stdout_digest))
    if stderr_digest is not None:
        parts.append(_field_bytes(8, stderr_digest))
    return b"".join(parts)


def _output_file(path: str, hash_value: str, size_bytes: int) -> bytes:
    return b"".join(
        [
            _field_bytes(1, path.encode()),
            _field_bytes(2, _digest(hash_value, size_bytes)),
            _field_varint(4, 1),
        ]
    )


def _output_directory(
    path: str, *, tree_digest: bytes | None = None, root_directory_digest: bytes | None = None
) -> bytes:
    parts = [_field_bytes(1, path.encode())]
    if tree_digest is not None:
        parts.append(_field_bytes(3, tree_digest))
    if root_directory_digest is not None:
        parts.append(_field_bytes(5, root_directory_digest))
    return b"".join(parts)


def _tree(*, root: bytes, children=()) -> bytes:
    parts = [_field_bytes(1, root)]
    for child in children:
        parts.append(_field_bytes(2, child))
    return b"".join(parts)


def _directory(*, files=(), directories=()) -> bytes:
    parts = []
    for file_node in files:
        parts.append(_field_bytes(1, file_node))
    for directory_node in directories:
        parts.append(_field_bytes(2, directory_node))
    return b"".join(parts)


def _file_node(name: str, hash_value: str, size_bytes: int) -> bytes:
    return b"".join(
        [
            _field_bytes(1, name.encode()),
            _field_bytes(2, _digest(hash_value, size_bytes)),
        ]
    )


def _directory_node(name: str, hash_value: str, size_bytes: int) -> bytes:
    return b"".join(
        [
            _field_bytes(1, name.encode()),
            _field_bytes(2, _digest(hash_value, size_bytes)),
        ]
    )


def _digest(hash_value: str, size_bytes: int) -> bytes:
    return b"".join(
        [
            _field_bytes(1, hash_value.encode()),
            _field_varint(2, size_bytes),
        ]
    )


def _field_bytes(field_number: int, value: bytes) -> bytes:
    return _varint((field_number << 3) | 2) + _varint(len(value)) + value


def _field_varint(field_number: int, value: int) -> bytes:
    return _varint(field_number << 3) + _varint(value)


def _varint(value: int) -> bytes:
    out = bytearray()
    current = value
    while current >= 0x80:
        out.append((current & 0x7F) | 0x80)
        current >>= 7
    out.append(current)
    return bytes(out)
