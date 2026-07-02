from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

from cost_insight.common.gcs_client import create_storage_client


@dataclass(frozen=True)
class GcsObjectMetadata:
    object_name: str
    exists: bool
    generation: int | None
    size_bytes: int | None = None


def fetch_object_metadata_batch(
    *,
    project_id: str,
    bucket_name: str,
    object_names: Iterable[str],
    max_workers: int = 64,
) -> tuple[GcsObjectMetadata, ...]:
    names = tuple(object_names)
    if not names:
        return ()

    client = create_storage_client(project_id=project_id, pool_maxsize=max_workers)
    bucket = client.bucket(bucket_name)

    def fetch_one(object_name: str) -> GcsObjectMetadata:
        blob = bucket.get_blob(object_name)
        if blob is None:
            return GcsObjectMetadata(
                object_name=object_name,
                exists=False,
                generation=None,
                size_bytes=None,
            )
        generation = int(blob.generation) if blob.generation is not None else None
        size_bytes = int(blob.size) if blob.size is not None else None
        return GcsObjectMetadata(
            object_name=object_name,
            exists=True,
            generation=generation,
            size_bytes=size_bytes,
        )

    if max_workers <= 1 or len(names) == 1:
        return tuple(fetch_one(name) for name in names)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return tuple(executor.map(fetch_one, names))
