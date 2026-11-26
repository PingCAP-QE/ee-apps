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
    kind: Literal["machine", "workload"] = "machine",
    machine_id: str | None = Query(None, max_length=512),
    region: str | None = Query(None, max_length=512),
    cluster_id: str | None = Query(None, max_length=512),
    resource_class: Literal["cvm", "tke_native"] | None = None,
    label_key: str | None = Query(None, max_length=64),
    label_value: str | None = Query(None, max_length=512),
    group_by: str | None = Query(None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=100),
    engine: Engine | None = Depends(utilization_engine),
) -> dict:
    return _call(
        request, queries.get_daily, engine, start_date=start_date, end_date=end_date,
        kind=kind, machine_id=machine_id, region=region, cluster_id=cluster_id, resource_class=resource_class,
        label_key=label_key, label_value=label_value, group_by=group_by,
        page=page, page_size=page_size,
    )


@router.get("/weekly")
def weekly(request: Request) -> dict:
    # No engine dependency: the retired endpoint must never access old summary tables.
    return _call(request, queries.get_weekly, None)


@router.get("/status")
def status(request: Request, engine: Engine | None = Depends(utilization_engine)) -> dict:
    return _call(request, queries.get_status, engine)
