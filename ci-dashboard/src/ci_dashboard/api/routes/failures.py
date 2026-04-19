from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.failures import (
    get_failure_category_share,
    get_failure_category_trend,
)
from ci_dashboard.api.routes.common import get_common_filters


router = APIRouter(prefix="/api/v1/failures", tags=["failures"])


@router.get("/category-trend")
def category_trend(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_failure_category_trend(engine, filters)


@router.get("/category-share")
def category_share(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_failure_category_share(engine, filters)
