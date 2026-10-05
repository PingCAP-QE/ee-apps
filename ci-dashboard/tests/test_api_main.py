from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ci_dashboard.api.main import app


def test_liveness_endpoint_is_async() -> None:
    route = next(route for route in app.routes if getattr(route, "path", None) == "/livez")
    assert inspect.iscoroutinefunction(route.endpoint)


def test_readiness_reports_healthy_database(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    monkeypatch.setattr("ci_dashboard.api.main.get_engine", lambda: engine)
    try:
        with TestClient(app) as client:
            response = client.get("/readyz")
    finally:
        engine.dispose()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_does_not_hide_unexpected_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_engine():
        raise AttributeError("unexpected bug")

    monkeypatch.setattr("ci_dashboard.api.main.get_engine", broken_engine)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/readyz")

    assert response.status_code == 500


def test_liveness_stays_healthy_when_database_config_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unavailable_engine():
        raise ValueError("missing database config")

    monkeypatch.setattr("ci_dashboard.api.main.get_engine", unavailable_engine)
    caplog.set_level("ERROR", logger="ci_dashboard.api.main")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/livez").json() == {"status": "ok"}
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "missing database config" in caplog.text
