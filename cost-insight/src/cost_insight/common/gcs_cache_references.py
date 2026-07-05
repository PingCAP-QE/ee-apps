from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock

from cost_insight.common.gcs_client import create_storage_client, resolve_storage_pool_maxsize


@dataclass(frozen=True)
class AcReferenceExtraction:
    ac_object_name: str
    exists: bool
    cas_object_names: tuple[str, ...]
    parse_error: str | None = None


@dataclass(frozen=True)
class _Digest:
    hash_value: str
    size_bytes: int


@dataclass(frozen=True)
class _WireField:
    field_number: int
    wire_type: int
    value: int | bytes


def extract_action_cache_references_batch(
    *,
    project_id: str,
    bucket_name: str,
    ac_object_names: Iterable[str],
    max_workers: int = 64,
    pool_maxsize: int | None = None,
) -> tuple[AcReferenceExtraction, ...]:
    from google.api_core.exceptions import NotFound

    names = tuple(ac_object_names)
    if not names:
        return ()

    client = create_storage_client(
        project_id=project_id,
        pool_maxsize=resolve_storage_pool_maxsize(
            max_workers=max_workers,
            pool_maxsize=pool_maxsize,
        ),
    )
    bucket = client.bucket(bucket_name)
    blob_cache: dict[str, bytes | None | Event | BaseException] = {}
    blob_cache_lock = Lock()

    def extract_one(ac_object_name: str) -> AcReferenceExtraction:
        blob = bucket.blob(ac_object_name)
        try:
            action_result_bytes = blob.download_as_bytes()
        except NotFound:
            return AcReferenceExtraction(
                ac_object_name=ac_object_name,
                exists=False,
                cas_object_names=(),
            )

        def fetch_cas_blob(hash_value: str) -> bytes | None:
            object_name = f"cas/{hash_value}"
            with blob_cache_lock:
                if object_name in blob_cache:
                    cached = blob_cache[object_name]
                    if not isinstance(cached, Event):
                        if isinstance(cached, BaseException):
                            raise cached
                        return cached
                    wait_for_download = cached
                    should_download = False
                else:
                    wait_for_download = Event()
                    blob_cache[object_name] = wait_for_download
                    should_download = True

            if not should_download:
                wait_for_download.wait()
                with blob_cache_lock:
                    cached = blob_cache[object_name]
                    if isinstance(cached, Event):
                        raise RuntimeError(f"CAS blob cache still pending for {object_name}")
                    if isinstance(cached, BaseException):
                        raise cached
                    return cached

            cas_blob = bucket.blob(object_name)
            try:
                data = cas_blob.download_as_bytes()
            except NotFound:
                data = None
            except BaseException as exc:
                with blob_cache_lock:
                    blob_cache[object_name] = exc
                    wait_for_download.set()
                raise

            with blob_cache_lock:
                blob_cache[object_name] = data
                wait_for_download.set()
                return data

        try:
            cas_object_names = tuple(
                sorted(
                    extract_cas_references_from_action_result_bytes(
                        action_result_bytes,
                        fetch_cas_blob=fetch_cas_blob,
                    )
                )
            )
        except ValueError as exc:
            return AcReferenceExtraction(
                ac_object_name=ac_object_name,
                exists=True,
                cas_object_names=(),
                parse_error=str(exc),
            )
        return AcReferenceExtraction(
            ac_object_name=ac_object_name,
            exists=True,
            cas_object_names=cas_object_names,
        )

    if max_workers <= 1 or len(names) == 1:
        return tuple(extract_one(name) for name in names)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return tuple(executor.map(extract_one, names))


def extract_cas_references_from_action_result_bytes(
    action_result_bytes: bytes,
    *,
    fetch_cas_blob: Callable[[str], bytes | None],
) -> set[str]:
    references: set[str] = set()
    for field in _parse_message(action_result_bytes):
        if field.field_number == 2 and isinstance(field.value, bytes):
            digest = _digest_from_output_file(field.value)
            if digest is not None:
                references.add(_cas_object_name(digest.hash_value))
        elif field.field_number in {7, 8} and isinstance(field.value, bytes):
            digest = _parse_digest(field.value)
            if digest is not None:
                references.add(_cas_object_name(digest.hash_value))
        elif field.field_number == 3 and isinstance(field.value, bytes):
            references.update(
                _references_from_output_directory(
                    field.value,
                    fetch_cas_blob=fetch_cas_blob,
                )
            )
    return references


def extract_cas_references_from_tree_bytes(tree_bytes: bytes) -> set[str]:
    references: set[str] = set()
    for field in _parse_message(tree_bytes):
        if field.field_number in {1, 2} and isinstance(field.value, bytes):
            references.update(_references_from_directory_bytes(field.value))
    return references


def _references_from_output_directory(
    output_directory_bytes: bytes,
    *,
    fetch_cas_blob: Callable[[str], bytes | None],
) -> set[str]:
    references: set[str] = set()
    root_directory_digests: list[_Digest] = []

    for field in _parse_message(output_directory_bytes):
        if field.field_number == 3 and isinstance(field.value, bytes):
            tree_digest = _parse_digest(field.value)
            if tree_digest is None:
                continue
            references.add(_cas_object_name(tree_digest.hash_value))
            tree_bytes = fetch_cas_blob(tree_digest.hash_value)
            if tree_bytes is not None:
                references.update(extract_cas_references_from_tree_bytes(tree_bytes))
        elif field.field_number == 5 and isinstance(field.value, bytes):
            root_directory_digest = _parse_digest(field.value)
            if root_directory_digest is not None:
                root_directory_digests.append(root_directory_digest)

    for digest in root_directory_digests:
        references.add(_cas_object_name(digest.hash_value))
        directory_bytes = fetch_cas_blob(digest.hash_value)
        if directory_bytes is None:
            continue
        references.update(
            _references_from_directory_bytes_recursive(
                directory_bytes,
                fetch_cas_blob=fetch_cas_blob,
                visited_hashes={digest.hash_value},
            )
        )

    return references


def _references_from_directory_bytes(directory_bytes: bytes) -> set[str]:
    references: set[str] = set()
    for field in _parse_message(directory_bytes):
        if field.field_number == 1 and isinstance(field.value, bytes):
            digest = _digest_from_file_node(field.value)
            if digest is not None:
                references.add(_cas_object_name(digest.hash_value))
    return references


def _references_from_directory_bytes_recursive(
    directory_bytes: bytes,
    *,
    fetch_cas_blob: Callable[[str], bytes | None],
    visited_hashes: set[str],
) -> set[str]:
    references = _references_from_directory_bytes(directory_bytes)

    for field in _parse_message(directory_bytes):
        if field.field_number != 2 or not isinstance(field.value, bytes):
            continue
        digest = _digest_from_directory_node(field.value)
        if digest is None:
            continue
        references.add(_cas_object_name(digest.hash_value))
        if digest.hash_value in visited_hashes:
            continue
        child_directory_bytes = fetch_cas_blob(digest.hash_value)
        if child_directory_bytes is None:
            continue
        references.update(
            _references_from_directory_bytes_recursive(
                child_directory_bytes,
                fetch_cas_blob=fetch_cas_blob,
                visited_hashes=visited_hashes | {digest.hash_value},
            )
        )

    return references


def _digest_from_output_file(output_file_bytes: bytes) -> _Digest | None:
    for field in _parse_message(output_file_bytes):
        if field.field_number == 2 and isinstance(field.value, bytes):
            return _parse_digest(field.value)
    return None


def _digest_from_file_node(file_node_bytes: bytes) -> _Digest | None:
    for field in _parse_message(file_node_bytes):
        if field.field_number == 2 and isinstance(field.value, bytes):
            return _parse_digest(field.value)
    return None


def _digest_from_directory_node(directory_node_bytes: bytes) -> _Digest | None:
    for field in _parse_message(directory_node_bytes):
        if field.field_number == 2 and isinstance(field.value, bytes):
            return _parse_digest(field.value)
    return None


def _parse_digest(digest_bytes: bytes) -> _Digest | None:
    hash_value: str | None = None
    size_bytes = 0
    for field in _parse_message(digest_bytes):
        if field.field_number == 1 and isinstance(field.value, bytes):
            hash_value = field.value.decode("utf-8")
        elif field.field_number == 2 and isinstance(field.value, int):
            size_bytes = int(field.value)
    if not hash_value:
        return None
    return _Digest(hash_value=hash_value, size_bytes=size_bytes)


def _cas_object_name(hash_value: str) -> str:
    return f"cas/{hash_value}"


def _parse_message(message_bytes: bytes) -> tuple[_WireField, ...]:
    fields: list[_WireField] = []
    offset = 0
    while offset < len(message_bytes):
        tag, offset = _read_varint(message_bytes, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value, offset = _read_varint(message_bytes, offset)
            fields.append(_WireField(field_number=field_number, wire_type=wire_type, value=value))
            continue
        if wire_type == 1:
            if offset + 8 > len(message_bytes):
                raise ValueError("Truncated protobuf fixed64 field")
            value = message_bytes[offset : offset + 8]
            offset += 8
            fields.append(_WireField(field_number=field_number, wire_type=wire_type, value=value))
            continue
        if wire_type == 2:
            length, offset = _read_varint(message_bytes, offset)
            if offset + length > len(message_bytes):
                raise ValueError("Truncated protobuf length-delimited field")
            value = message_bytes[offset : offset + length]
            offset += length
            fields.append(_WireField(field_number=field_number, wire_type=wire_type, value=value))
            continue
        if wire_type == 5:
            if offset + 4 > len(message_bytes):
                raise ValueError("Truncated protobuf fixed32 field")
            value = message_bytes[offset : offset + 4]
            offset += 4
            fields.append(_WireField(field_number=field_number, wire_type=wire_type, value=value))
            continue
        raise ValueError(f"Unsupported protobuf wire type: {wire_type}")
    return tuple(fields)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    result = 0
    for _ in range(10):
        if offset >= len(data):
            raise ValueError("Unexpected end of protobuf varint")
        current = data[offset]
        offset += 1
        result |= (current & 0x7F) << shift
        if current < 0x80:
            return result, offset
        shift += 7
    raise ValueError("Protobuf varint exceeds 10 bytes")
