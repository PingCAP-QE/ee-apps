from __future__ import annotations

from ci_dashboard.api.main import app, healthz, livez, readyz


def test_probe_endpoints_return_ok() -> None:
    assert app.title == "CI Dashboard"
    assert healthz() == {"status": "ok"}
    assert livez() == {"status": "ok"}
    assert readyz() == {"status": "ok"}
