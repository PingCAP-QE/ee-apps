from __future__ import annotations

import logging
import os
from hashlib import sha256
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from ci_dashboard import __version__
from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.routes.builds import router as builds_router
from ci_dashboard.api.routes.failures import router as failures_router
from ci_dashboard.api.routes.filters import router as filters_router
from ci_dashboard.api.routes.flaky import router as flaky_router
from ci_dashboard.api.routes.pages import router as pages_router
from ci_dashboard.api.routes.status import router as status_router


LOG = logging.getLogger(__name__)


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def livez() -> dict[str, str]:
    return {"status": "ok"}


def _dashboard_version() -> str:
    return (os.environ.get("CI_DASHBOARD_VERSION") or "").strip()


def _check_database() -> None:
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1")).scalar_one()


async def readyz() -> dict[str, str]:
    try:
        await run_in_threadpool(_check_database)
    except (SQLAlchemyError, ValueError):
        LOG.exception("database readiness check failed")
        raise HTTPException(status_code=503, detail="database unavailable") from None
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

    index_file = static_dir / "index.html"
    cached_index: tuple[tuple[int, int, str], str, str] | None = None

    def frontend_index() -> tuple[str, str] | None:
        nonlocal cached_index
        if not index_file.is_file():
            return None
        stat = index_file.stat()
        cache_key = (stat.st_mtime_ns, stat.st_size, _dashboard_version())
        if cached_index is None or cached_index[0] != cache_key:
            content = index_file.read_text(encoding="utf-8").replace(
                "__CI_DASHBOARD_VERSION__",
                escape(cache_key[2], quote=True),
            )
            etag = f'"{sha256(":".join(map(str, cache_key)).encode()).hexdigest()}"'
            cached_index = (cache_key, content, etag)
        return cached_index[1], cached_index[2]

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def frontend(full_path: str, request: Request) -> FileResponse | HTMLResponse | Response:
        if full_path.startswith("api/") or full_path in {"healthz", "livez", "readyz"}:
            raise HTTPException(status_code=404, detail="Not found")

        if full_path:
            candidate = (static_dir / full_path).resolve()
            if (
                candidate.is_file()
                and candidate.is_relative_to(static_dir)
                and candidate != index_file
            ):
                return FileResponse(candidate)

        rendered_index = frontend_index()
        if rendered_index is None:
            raise HTTPException(status_code=404, detail="Frontend build not found")
        content, etag = rendered_index
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        validators = {value.strip() for value in request.headers.get("if-none-match", "").split(",")}
        if "*" in validators or etag in validators:
            return Response(status_code=304, headers=headers)
        return HTMLResponse(content, headers=headers)


def create_app() -> FastAPI:
    app = FastAPI(title="CI Dashboard", version=__version__)

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
