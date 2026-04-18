from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.pages import (
    get_build_trend_page,
    get_flaky_page,
    get_overview_page,
)
from ci_dashboard.api.routes.common import get_common_filters


router = APIRouter(prefix="/api/v1/pages", tags=["pages"])


@router.get("/overview")
def overview_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_overview_page(engine, filters)


@router.get("/ci-status")
def ci_status_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_build_trend_page(engine, filters)


@router.get("/flaky")
def flaky_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_flaky_page(engine, filters)
