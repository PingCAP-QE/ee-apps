from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.filters import (
    list_branches,
    list_cloud_phases,
    list_jobs,
    list_repos,
)


router = APIRouter(prefix="/api/v1/filters", tags=["filters"])


@router.get("/repos")
def repos(
    start_date: date | None = None,
    end_date: date | None = None,
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return list_repos(engine, start_date=start_date, end_date=end_date)


@router.get("/branches")
def branches(
    repo: str | None = None,
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return list_branches(engine, repo=repo)


@router.get("/jobs")
def jobs(
    repo: str | None = None,
    branch: str | None = None,
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return list_jobs(engine, repo=repo, branch=branch)


@router.get("/cloud-phases")
def cloud_phases(engine: Engine = Depends(get_engine)) -> dict[str, object]:
    return list_cloud_phases(engine)
