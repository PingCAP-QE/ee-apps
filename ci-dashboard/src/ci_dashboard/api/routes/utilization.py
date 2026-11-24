from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries import utilization as queries

router = APIRouter(prefix="/api/v1/utilization", tags=["utilization"])


def utilization_engine() -> Engine | None:
    # Disabled deployments need neither the migration nor a configured cost database.
    if not queries.is_enabled():
        return None
    try:
        return get_engine()
    except (SQLAlchemyError, ValueError):
        return None


def _call(request: Request, function, engine, **params) -> dict:
    if set(request.query_params) - set(params):
        raise HTTPException(status_code=422, detail="unsupported utilization filter")
    try:
        return function(engine, **params)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.get("/daily")
def daily(
    request: Request,
    start_date: date | None = None,
    end_date: date | None = None,
    scope: Literal["resource", "job", "repo", "account_class"] = "job",
    repo: str | None = Query(None, max_length=512),
    job_name: str | None = Query(None, max_length=512),
    resource_class: str | None = Query(None, max_length=512),
    resource_spec: str | None = Query(None, max_length=512),
    resource_key: str | None = Query(None, max_length=64),
    page: int = Query(1, ge=1, le=100),
    page_size: int = Query(50, ge=1, le=100),
    engine: Engine | None = Depends(utilization_engine),
) -> dict:
    return _call(
        request,
        queries.get_daily,
        engine,
        start_date=start_date,
        end_date=end_date,
        scope=scope,
        repo=repo,
        job_name=job_name,
        resource_class=resource_class,
        resource_spec=resource_spec,
        resource_key=resource_key,
        page=page,
        page_size=page_size,
    )


@router.get("/weekly")
def weekly(
    request: Request,
    start_date: date | None = None,
    end_date: date | None = None,
    resource_class: str | None = Query(None, max_length=512),
    metric: Literal["cpu", "memory", "cpu_allocation", "memory_allocation"] | None = None,
    page: int = Query(1, ge=1, le=100),
    page_size: int = Query(100, ge=1, le=100),
    engine: Engine | None = Depends(utilization_engine),
) -> dict:
    return _call(
        request,
        queries.get_weekly,
        engine,
        start_date=start_date,
        end_date=end_date,
        resource_class=resource_class,
        metric=metric,
        page=page,
        page_size=page_size,
    )


@router.get("/status")
def status(request: Request, engine: Engine | None = Depends(utilization_engine)) -> dict:
    return _call(request, queries.get_status, engine)
