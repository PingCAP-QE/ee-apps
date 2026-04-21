from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.engine import Engine

from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.builds import (
    get_cloud_comparison,
    get_cloud_posture_trend,
    get_cloud_repo_share,
    get_duration_trend,
    get_longest_avg_success_jobs,
    get_migration_runtime_comparison,
    get_lowest_success_rate_jobs,
    get_outcome_trend,
)
from ci_dashboard.api.queries.failures import (
    get_failure_category_share,
    get_failure_category_trend,
)
from ci_dashboard.api.queries.filters import list_repos
from ci_dashboard.api.queries.flaky import (
    get_distinct_flaky_case_counts_by_branch,
    get_flaky_composition,
    get_issue_lifecycle_snapshot,
    get_issue_lifecycle_weekly,
    get_flaky_period_comparison,
    get_flaky_top_jobs,
    get_flaky_trend,
    get_issue_filtered_weekly_case_rates,
)
from ci_dashboard.api.queries.status import get_freshness


def get_overview_page(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    previous_start, previous_end = _get_previous_date_range(filters)
    return {
        "scope": filters.meta(),
        "freshness": get_freshness(engine),
        "repos": list_repos(
            engine,
            start_date=filters.start_date,
            end_date=filters.end_date,
        ),
        "outcome_trend": get_outcome_trend(engine, filters),
        "top_noisy_jobs": get_flaky_top_jobs(engine, filters, limit=5),
        "failure_category_share": get_failure_category_share(engine, filters),
        "cloud_comparison": get_cloud_comparison(engine, filters),
        "period_comparison": (
            get_flaky_period_comparison(
                engine,
                repo=filters.repo,
                branch=filters.branch,
                job_name=filters.job_name,
                cloud_phase=filters.cloud_phase,
                period_a_start=filters.start_date,
                period_a_end=filters.end_date,
                period_b_start=previous_start,
                period_b_end=previous_end,
            )
            if previous_start and previous_end and filters.start_date and filters.end_date
            else {"groups": [], "meta": filters.meta()}
        ),
    }


def get_build_trend_page(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    repo_share_filters = filters.without_cloud_phase().without_repo()
    migration_filters = CommonFilters(
        repo=filters.repo,
        branch=filters.branch,
        job_name=filters.job_name,
        cloud_phase=None,
        issue_status=None,
        start_date=None,
        end_date=filters.end_date,
        granularity=filters.granularity,
    )
    return {
        "scope": filters.meta(),
        "outcome_trend": get_outcome_trend(engine, filters),
        "duration_trend": get_duration_trend(engine, filters),
        "cloud_posture_trend": get_cloud_posture_trend(engine, filters),
        "longest_avg_success_jobs": get_longest_avg_success_jobs(engine, filters),
        "lowest_success_rate_jobs": get_lowest_success_rate_jobs(engine, filters),
        "migration_runtime_comparison": get_migration_runtime_comparison(engine, migration_filters),
        "cloud_repo_share": get_cloud_repo_share(engine, repo_share_filters),
    }


def get_flaky_page(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    previous_start, previous_end = _get_previous_date_range(filters)
    build_scope_filters = filters.without_issue_status()
    build_scope_filters_weekly = CommonFilters(
        repo=build_scope_filters.repo,
        branch=build_scope_filters.branch,
        job_name=build_scope_filters.job_name,
        cloud_phase=build_scope_filters.cloud_phase,
        issue_status=None,
        start_date=build_scope_filters.start_date,
        end_date=build_scope_filters.end_date,
        granularity="week",
    )
    issue_case_rates = get_issue_filtered_weekly_case_rates(engine, filters)
    return {
        "scope": filters.meta(),
        "distinct_flaky_case_counts": get_distinct_flaky_case_counts_by_branch(engine, filters),
        "issue_lifecycle": get_issue_lifecycle_snapshot(engine, filters),
        "issue_lifecycle_weekly": get_issue_lifecycle_weekly(engine, filters),
        "issue_case_weekly_rates": {
            "weeks": issue_case_rates["weeks"],
            "rows": issue_case_rates["rows"],
            "meta": issue_case_rates["meta"],
        },
        "issue_filtered_weekly_trend": issue_case_rates["trend"],
        "trend": get_flaky_trend(engine, build_scope_filters),
        "composition": get_flaky_composition(engine, build_scope_filters_weekly),
        "top_jobs": get_flaky_top_jobs(engine, build_scope_filters, limit=8),
        "failure_category_share": get_failure_category_share(engine, build_scope_filters),
        "failure_category_trend": get_failure_category_trend(engine, build_scope_filters),
        "period_comparison": (
            get_flaky_period_comparison(
                engine,
                repo=build_scope_filters.repo,
                branch=build_scope_filters.branch,
                job_name=build_scope_filters.job_name,
                cloud_phase=build_scope_filters.cloud_phase,
                period_a_start=build_scope_filters.start_date,
                period_a_end=build_scope_filters.end_date,
                period_b_start=previous_start,
                period_b_end=previous_end,
            )
            if previous_start and previous_end and filters.start_date and filters.end_date
            else {"groups": [], "meta": filters.meta()}
        ),
    }


def _get_previous_date_range(filters: CommonFilters) -> tuple[date | None, date | None]:
    if filters.start_date is None or filters.end_date is None:
        return None, None

    span_days = max((filters.end_date - filters.start_date).days + 1, 1)
    previous_end = filters.start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span_days - 1)
    return previous_start, previous_end
