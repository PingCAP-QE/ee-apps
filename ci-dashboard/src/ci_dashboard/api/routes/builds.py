from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.builds import (
    get_cloud_comparison,
    get_duration_trend,
    get_migration_runtime_comparison,
    get_outcome_trend,
)
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.routes.common import get_common_filters


router = APIRouter(prefix="/api/v1/builds", tags=["builds"])


@router.get("/outcome-trend")
def outcome_trend(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_outcome_trend(engine, filters)


@router.get("/duration-trend")
def duration_trend(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_duration_trend(engine, filters)


@router.get("/cloud-comparison")
def cloud_comparison(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cloud_comparison(engine, filters)


@router.get("/migration-runtime-comparison")
def migration_runtime_comparison(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_migration_runtime_comparison(engine, filters)
