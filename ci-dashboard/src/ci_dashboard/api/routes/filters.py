from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.filters import (
    list_branches,
    list_cloud_phases,
    list_jobs,
    list_repos,
)
from ci_dashboard.api.routes.common import get_common_filters


router = APIRouter(prefix="/api/v1/filters", tags=["filters"])


@router.get("/repos")
def repos(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return list_repos(engine, start_date=filters.start_date, end_date=filters.end_date)


@router.get("/branches")
def branches(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return list_branches(
        engine, repo=filters.repo, start_date=filters.start_date, end_date=filters.end_date,
    )


@router.get("/jobs")
def jobs(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return list_jobs(
        engine,
        repo=filters.repo,
        branch=filters.branch,
        start_date=filters.start_date,
        end_date=filters.end_date,
    )


@router.get("/cloud-phases")
def cloud_phases(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return list_cloud_phases(
        engine,
        repo=filters.repo,
        branch=filters.branch,
        job_name=filters.job_name,
        start_date=filters.start_date,
        end_date=filters.end_date,
    )
