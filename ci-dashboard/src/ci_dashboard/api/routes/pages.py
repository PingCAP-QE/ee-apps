from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.base import MAX_RANKING_LIMIT
from ci_dashboard.api.queries.pages import (
    get_build_trend_page,
    get_cost_engineering_group_share_page,
    get_cost_insight_page,
    get_cost_repo_group_stack_page,
    get_cost_trend_page,
    get_cost_unmatched_resources_page,
    get_cost_weekly_overview_page,
    get_flaky_page,
    get_overview_page,
    get_runtime_insights_page,
)
from ci_dashboard.api.queries.runtime import get_error_builds, get_error_top_jobs
from ci_dashboard.api.routes.common import get_common_filters
from ci_dashboard.common.config import Settings, get_settings


router = APIRouter(prefix="/api/v1/pages", tags=["pages"])


@router.get("/navigation")
def navigation_page(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "features": {
            "runtime_insights_enabled": settings.features.runtime_insights_enabled,
            "cost_dashboard_enabled": settings.features.cost_dashboard_enabled,
        }
    }


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


@router.get("/runtime-insights")
def runtime_insights_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_runtime_insights_page(engine, filters)


@router.get("/cost")
def cost_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_insight_page(engine, filters)


@router.get("/cost-trend")
def cost_trend_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_trend_page(engine, filters)


@router.get("/cost-weekly-overview")
def cost_weekly_overview_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_weekly_overview_page(engine, filters)


@router.get("/cost-repo-group-stack")
def cost_repo_group_stack_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_repo_group_stack_page(engine, filters)


@router.get("/cost-engineering-group-share")
def cost_engineering_group_share_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_engineering_group_share_page(engine, filters)


@router.get("/cost-unmatched-resources")
def cost_unmatched_resources_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_unmatched_resources_page(engine, filters)


@router.get("/runtime-error-top-jobs")
def runtime_error_top_jobs(
    filters: CommonFilters = Depends(get_common_filters),
    error_l1_category: str | None = None,
    error_l2_subcategory: str | None = None,
    limit: int = Query(default=10),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_error_top_jobs(
        engine,
        filters,
        limit=min(limit, MAX_RANKING_LIMIT),
        l1_category=error_l1_category,
        l2_subcategory=error_l2_subcategory,
    )


@router.get("/runtime-error-builds")
def runtime_error_builds(
    filters: CommonFilters = Depends(get_common_filters),
    selected_job_name: str | None = None,
    error_l1_category: str | None = None,
    error_l2_subcategory: str | None = None,
    limit: int = Query(default=15),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_error_builds(
        engine,
        filters,
        job_name=selected_job_name,
        limit=min(limit, MAX_RANKING_LIMIT),
        l1_category=error_l1_category,
        l2_subcategory=error_l2_subcategory,
    )
