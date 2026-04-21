from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.base import CommonFilters, MAX_RANKING_LIMIT
from ci_dashboard.api.queries.flaky import (
    get_flaky_case_flow_v2,
    get_distinct_flaky_case_counts_by_branch,
    get_flaky_composition,
    get_flaky_period_comparison,
    get_flaky_top_jobs,
    get_flaky_trend,
    get_issue_filtered_weekly_case_rates,
)
from ci_dashboard.api.routes.common import get_common_filters, validate_date_range


router = APIRouter(prefix="/api/v1/flaky", tags=["flaky"])


@router.get("/trend")
def flaky_trend(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_flaky_trend(engine, filters)


@router.get("/composition")
def flaky_composition(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_flaky_composition(engine, filters)


@router.get("/distinct-case-counts")
def distinct_flaky_case_counts(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_distinct_flaky_case_counts_by_branch(engine, filters)


@router.get("/case-flow-v2")
def flaky_case_flow_v2(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_flaky_case_flow_v2(engine, filters)


@router.get("/issue-weekly-rates")
def issue_weekly_rates(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_issue_filtered_weekly_case_rates(engine, filters)


@router.get("/top-jobs")
def flaky_top_jobs(
    filters: CommonFilters = Depends(get_common_filters),
    limit: int = Query(default=10),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be positive")
    return get_flaky_top_jobs(engine, filters, limit=min(limit, MAX_RANKING_LIMIT))


@router.get("/period-comparison")
def flaky_period_comparison(
    period_a_start: date,
    period_a_end: date,
    period_b_start: date,
    period_b_end: date,
    repo: str | None = None,
    branch: str | None = None,
    job_name: str | None = None,
    cloud_phase: str | None = None,
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    validate_date_range(period_a_start, period_a_end)
    validate_date_range(period_b_start, period_b_end)
    return get_flaky_period_comparison(
        engine,
        repo=repo,
        branch=branch,
        job_name=job_name,
        cloud_phase=cloud_phase,
        period_a_start=period_a_start,
        period_a_end=period_a_end,
        period_b_start=period_b_start,
        period_b_end=period_b_end,
    )
