from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ci_dashboard.api.routes.builds import router as builds_router
from ci_dashboard.api.routes.failures import router as failures_router
from ci_dashboard.api.routes.filters import router as filters_router
from ci_dashboard.api.routes.flaky import router as flaky_router
from ci_dashboard.api.routes.pages import router as pages_router
from ci_dashboard.api.routes.status import router as status_router


def healthz() -> dict[str, str]:
    return {"status": "ok"}


def livez() -> dict[str, str]:
    return {"status": "ok"}


def readyz() -> dict[str, str]:
    return {"status": "ok"}


def _resolve_frontend_static_dir() -> Path:
    configured_dir = (os.environ.get("CI_DASHBOARD_STATIC_DIR") or "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser().resolve()

    module_path = Path(__file__).resolve()
    candidates: list[Path] = [
        *((parent / "web" / "dist") for parent in module_path.parents),
        Path.cwd() / "web" / "dist",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            return resolved
    return (Path.cwd() / "web" / "dist").resolve()


def _attach_frontend(app: FastAPI) -> None:
    static_dir = _resolve_frontend_static_dir()
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path in {"healthz", "livez", "readyz"}:
            raise HTTPException(status_code=404, detail="Not found")

        if full_path:
            candidate = (static_dir / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(static_dir):
                return FileResponse(candidate)

        index_file = static_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        raise HTTPException(status_code=404, detail="Frontend build not found")


def create_app() -> FastAPI:
    app = FastAPI(title="CI Dashboard", version="0.1.2")

    app.include_router(status_router)
    app.include_router(filters_router)
    app.include_router(flaky_router)
    app.include_router(builds_router)
    app.include_router(failures_router)
    app.include_router(pages_router)
    app.get("/healthz")(healthz)
    app.get("/livez")(livez)
    app.get("/readyz")(readyz)
    _attach_frontend(app)

    return app


app = create_app()
