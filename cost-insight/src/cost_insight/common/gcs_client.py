from __future__ import annotations

import logging

LOG = logging.getLogger(__name__)


def create_storage_client(*, project_id: str, pool_maxsize: int):
    from google.cloud import storage

    client = storage.Client(project=project_id)
    configure_storage_client_http_pool(client=client, pool_maxsize=pool_maxsize)
    return client


def resolve_storage_pool_maxsize(*, max_workers: int, pool_maxsize: int | None) -> int:
    if pool_maxsize is None:
        return max_workers
    if pool_maxsize <= 0:
        return pool_maxsize
    return max(max_workers, pool_maxsize)


def configure_storage_client_http_pool(*, client, pool_maxsize: int) -> None:
    if pool_maxsize <= 0:
        return

    session = getattr(client, "_http", None)
    if session is None:
        LOG.warning(
            "GCS client HTTP pool expansion skipped because storage.Client._http is unavailable"
        )
        return

    adapters = getattr(session, "adapters", None)
    if not isinstance(adapters, dict):
        LOG.warning(
            "GCS client HTTP pool expansion skipped because storage.Client._http.adapters is unavailable"
        )
        return

    from requests.adapters import HTTPAdapter

    for prefix in ("https://", "http://"):
        current_adapter = adapters.get(prefix)
        pool_connections = getattr(current_adapter, "_pool_connections", 10)
        max_retries = getattr(current_adapter, "max_retries", 0)
        session.mount(
            prefix,
            HTTPAdapter(
                pool_connections=pool_connections,
                pool_maxsize=pool_maxsize,
                max_retries=max_retries,
            ),
        )
