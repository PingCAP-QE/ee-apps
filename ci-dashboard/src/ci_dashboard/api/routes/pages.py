from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.base import MAX_RANKING_LIMIT
from ci_dashboard.api.queries.cost import COST_DRILLDOWN_CHILD_GROUPS
from ci_dashboard.api.queries.pages import (
    get_build_trend_page,
    get_cost_engineering_group_share_page,
    get_cost_insight_page,
    get_cost_unattached_block_volumes_page,
    get_cost_sources_page,
    get_cost_share_page,
    get_cost_repo_group_stack_page,
    get_cost_trend_page,
    get_cost_unmatched_resources_page,
    get_cost_unattached_ebs_volumes_page,
    get_cost_weekly_account_summaries_page,
    get_cost_weekly_overview_page,
    get_flaky_page,
    get_overview_page,
    get_runtime_insights_page,
)
from ci_dashboard.api.queries.runtime import get_error_builds, get_error_top_jobs
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


@router.get("/cost-sources")
def cost_sources_page(
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_sources_page(engine)


@router.get("/cost-trend")
def cost_trend_page(
    drilldown_group: str | None = Query(default=None, pattern="^(team|cost_driver)$"),
    drilldown_value: str | None = None,
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_trend_page(
        engine,
        filters,
        drilldown_group=drilldown_group,
        drilldown_value=drilldown_value,
    )


@router.get("/cost-share")
def cost_share_page(
    dimension: str = Query(
        "owner",
        pattern="^(owner|team|service|sku|cost_driver|project|service_exec_id|region)$",
    ),
    drilldown_group: str | None = Query(default=None, pattern="^(team|cost_driver)$"),
    drilldown_value: str | None = None,
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    _validate_cost_drilldown_child(dimension, drilldown_group)
    return get_cost_share_page(
        engine,
        filters,
        dimension=dimension,
        drilldown_group=drilldown_group,
        drilldown_value=drilldown_value,
    )


@router.get("/cost-weekly-overview")
def cost_weekly_overview_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_weekly_overview_page(engine, filters)


@router.get("/cost-weekly-account-summaries")
def cost_weekly_account_summaries_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_weekly_account_summaries_page(engine, filters)


@router.get("/cost-repo-group-stack")
def cost_repo_group_stack_page(
    group_by: str = Query(
        "repo",
        pattern="^(repo|author|owner|team|target_branch|service|sku|cost_driver|project|region|service_exec_id)$",
    ),
    drilldown_group: str | None = Query(default=None, pattern="^(team|cost_driver)$"),
    drilldown_value: str | None = None,
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    _validate_cost_drilldown_child(group_by, drilldown_group)
    return get_cost_repo_group_stack_page(
        engine,
        filters,
        group_by=group_by,
        drilldown_group=drilldown_group,
        drilldown_value=drilldown_value,
    )


@router.get("/cost-engineering-group-share")
def cost_engineering_group_share_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_engineering_group_share_page(engine, filters)


@router.get("/cost-unmatched-resources")
def cost_unmatched_resources_page(
    service_name: str | None = None,
    sort_by: str = Query("list_cost", pattern="^(list_cost|duration)$"),
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_unmatched_resources_page(
        engine,
        filters,
        service_name=service_name,
        sort_by=sort_by,
    )


@router.get("/cost-unattached-ebs-volumes")
def cost_unattached_ebs_volumes_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_unattached_ebs_volumes_page(engine, filters)


@router.get("/cost-unattached-block-volumes")
def cost_unattached_block_volumes_page(
    filters: CommonFilters = Depends(get_common_filters),
    engine: Engine = Depends(get_engine),
) -> dict[str, object]:
    return get_cost_unattached_block_volumes_page(engine, filters)


def _validate_cost_drilldown_child(child_group: str, drilldown_group: str | None) -> None:
    if not drilldown_group:
        return
    expected_child = COST_DRILLDOWN_CHILD_GROUPS.get(drilldown_group)
    if expected_child != child_group:
        raise HTTPException(
            status_code=400,
            detail=f"drilldown_group={drilldown_group!r} requires {expected_child!r} child group",
        )


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
