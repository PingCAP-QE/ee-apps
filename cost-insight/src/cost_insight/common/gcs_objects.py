from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class GcsObjectMetadata:
    object_name: str
    exists: bool
    generation: int | None


def fetch_object_metadata_batch(
    *,
    project_id: str,
    bucket_name: str,
    object_names: Iterable[str],
    max_workers: int = 64,
) -> tuple[GcsObjectMetadata, ...]:
    from google.cloud import storage

    names = tuple(object_names)
    if not names:
        return ()

    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)

    def fetch_one(object_name: str) -> GcsObjectMetadata:
        blob = bucket.get_blob(object_name)
        if blob is None:
            return GcsObjectMetadata(
                object_name=object_name,
                exists=False,
                generation=None,
            )
        generation = int(blob.generation) if blob.generation is not None else None
        return GcsObjectMetadata(
            object_name=object_name,
            exists=True,
            generation=generation,
        )

    if max_workers <= 1 or len(names) == 1:
        return tuple(fetch_one(name) for name in names)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return tuple(executor.map(fetch_one, names))
