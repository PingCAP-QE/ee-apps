from __future__ import annotations

import logging
import sys
from types import ModuleType

import pytest

from cost_insight.common import gcs_client


def test_create_storage_client_expands_http_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    mounted: list[tuple[str, object]] = []

    class _FakeAdapter:
        def __init__(self, *, pool_connections: int = 10, pool_maxsize: int = 10, max_retries: int = 3) -> None:
            self._pool_connections = pool_connections
            self._pool_maxsize = pool_maxsize
            self.max_retries = max_retries

    class _MountedAdapter(_FakeAdapter):
        pass

    class _FakeSession:
        def __init__(self) -> None:
            self.adapters = {
                "https://": _FakeAdapter(pool_connections=10, pool_maxsize=10, max_retries=5),
                "http://": _FakeAdapter(pool_connections=8, pool_maxsize=8, max_retries=2),
            }

        def mount(self, prefix: str, adapter: object) -> None:
            mounted.append((prefix, adapter))
            self.adapters[prefix] = adapter

    class _FakeClient:
        def __init__(self, *, project: str) -> None:
            self.project = project
            self._http = _FakeSession()

    cloud_module = ModuleType("google.cloud")
    storage_module = ModuleType("google.cloud.storage")
    requests_module = ModuleType("requests")
    requests_adapters_module = ModuleType("requests.adapters")

    def _client_factory(project=None):
        return _FakeClient(project=project)

    def _adapter_factory(*, pool_connections: int, pool_maxsize: int, max_retries):
        return _MountedAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=max_retries,
        )

    storage_module.Client = _client_factory
    cloud_module.storage = storage_module
    requests_adapters_module.HTTPAdapter = _adapter_factory
    requests_module.adapters = requests_adapters_module

    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_module)
    monkeypatch.setitem(sys.modules, "requests", requests_module)
    monkeypatch.setitem(sys.modules, "requests.adapters", requests_adapters_module)

    client = gcs_client.create_storage_client(project_id="test-project", pool_maxsize=32)

    assert client.project == "test-project"
    assert [prefix for prefix, _ in mounted] == ["https://", "http://"]
    https_adapter = client._http.adapters["https://"]
    http_adapter = client._http.adapters["http://"]
    assert https_adapter._pool_connections == 10
    assert https_adapter._pool_maxsize == 32
    assert https_adapter.max_retries == 5
    assert http_adapter._pool_connections == 8
    assert http_adapter._pool_maxsize == 32
    assert http_adapter.max_retries == 2


def test_configure_storage_client_http_pool_warns_when_http_session_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FakeClient:
        pass

    with caplog.at_level(logging.WARNING):
        gcs_client.configure_storage_client_http_pool(client=_FakeClient(), pool_maxsize=32)

    assert "storage.Client._http is unavailable" in caplog.text
