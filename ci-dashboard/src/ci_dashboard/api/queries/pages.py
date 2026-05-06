from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Callable

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
    get_issue_fix_progress_snapshot,
    get_issue_lifecycle_snapshot,
    get_issue_lifecycle_weekly,
    get_flaky_period_comparison,
    get_flaky_top_jobs,
    get_flaky_trend,
    get_issue_filtered_weekly_case_rates,
)
from ci_dashboard.api.queries.runtime import (
    get_classification_coverage,
    get_error_l1_share,
    get_error_l1_trend,
    get_error_l2_trends,
    get_error_top_jobs,
    get_infra_l2_share,
    get_infra_l2_trend,
    get_pull_image_failure_jobs,
    get_pull_image_slowest_jobs,
    get_pull_image_trend,
    get_runtime_summary,
    get_scheduling_failure_jobs,
    get_scheduling_slowest_jobs,
    get_scheduling_trend,
)
from ci_dashboard.api.queries.status import get_freshness


def get_overview_page(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    previous_start, previous_end = _get_previous_date_range(filters)
    sections = _resolve_page_sections(
        engine,
        {
            "freshness": lambda: get_freshness(engine),
            "repos": lambda: list_repos(
                engine,
                start_date=filters.start_date,
                end_date=filters.end_date,
            ),
            "outcome_trend": lambda: get_outcome_trend(engine, filters),
            "top_noisy_jobs": lambda: get_flaky_top_jobs(engine, filters, limit=5),
            "failure_category_share": lambda: get_failure_category_share(engine, filters),
            "cloud_comparison": lambda: get_cloud_comparison(engine, filters),
            "period_comparison": (
                lambda: get_flaky_period_comparison(
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
    )
    return {
        "scope": filters.meta(),
        **sections,
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
    sections = _resolve_page_sections(
        engine,
        {
            "outcome_trend": lambda: get_outcome_trend(engine, filters),
            "duration_trend": lambda: get_duration_trend(engine, filters),
            "cloud_posture_trend": lambda: get_cloud_posture_trend(engine, filters),
            "longest_avg_success_jobs": lambda: get_longest_avg_success_jobs(engine, filters),
            "lowest_success_rate_jobs": lambda: get_lowest_success_rate_jobs(engine, filters),
            "migration_runtime_comparison": lambda: get_migration_runtime_comparison(
                engine,
                migration_filters,
            ),
            "cloud_repo_share": lambda: get_cloud_repo_share(engine, repo_share_filters),
        }
    )
    return {
        "scope": filters.meta(),
        **sections,
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
    sections = _resolve_page_sections(
        engine,
        {
            "distinct_flaky_case_counts": lambda: get_distinct_flaky_case_counts_by_branch(
                engine,
                filters,
            ),
            "issue_fix_progress": lambda: get_issue_fix_progress_snapshot(engine, filters),
            "issue_lifecycle": lambda: get_issue_lifecycle_snapshot(engine, filters),
            "issue_lifecycle_weekly": lambda: get_issue_lifecycle_weekly(engine, filters),
            "issue_case_rates": lambda: get_issue_filtered_weekly_case_rates(engine, filters),
            "trend": lambda: get_flaky_trend(engine, build_scope_filters),
            "composition": lambda: get_flaky_composition(engine, build_scope_filters_weekly),
            "top_jobs": lambda: get_flaky_top_jobs(engine, build_scope_filters, limit=8),
            "failure_category_share": lambda: get_failure_category_share(
                engine,
                build_scope_filters,
            ),
            "failure_category_trend": lambda: get_failure_category_trend(
                engine,
                build_scope_filters,
            ),
            "period_comparison": (
                lambda: get_flaky_period_comparison(
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
    )
    issue_case_rates = sections.pop("issue_case_rates")
    return {
        "scope": filters.meta(),
        **sections,
        "issue_case_weekly_rates": {
            "weeks": issue_case_rates["weeks"],
            "rows": issue_case_rates["rows"],
            "meta": issue_case_rates["meta"],
        },
        "issue_filtered_weekly_trend": issue_case_rates["trend"],
    }


def get_runtime_insights_page(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    sections = _resolve_page_sections(
        engine,
        {
            "runtime_summary": lambda: get_runtime_summary(engine, filters),
            "scheduling_trend": lambda: get_scheduling_trend(engine, filters),
            "scheduling_failure_jobs": lambda: get_scheduling_failure_jobs(engine, filters),
            "scheduling_slowest_jobs": lambda: get_scheduling_slowest_jobs(engine, filters),
            "pull_image_trend": lambda: get_pull_image_trend(engine, filters),
            "pull_image_failure_jobs": lambda: get_pull_image_failure_jobs(engine, filters),
            "pull_image_slowest_jobs": lambda: get_pull_image_slowest_jobs(engine, filters),
            "error_l1_share": lambda: get_error_l1_share(engine, filters),
            "error_l1_trend": lambda: get_error_l1_trend(engine, filters),
            "error_l2_trends": lambda: get_error_l2_trends(engine, filters),
            "infra_l2_share": lambda: get_infra_l2_share(engine, filters),
            "infra_l2_trend": lambda: get_infra_l2_trend(engine, filters),
            "error_top_jobs": lambda: get_error_top_jobs(engine, filters),
            "classification_coverage": lambda: get_classification_coverage(engine, filters),
        },
    )
    return {
        "scope": filters.meta(),
        **sections,
    }


def _get_previous_date_range(filters: CommonFilters) -> tuple[date | None, date | None]:
    if filters.start_date is None or filters.end_date is None:
        return None, None

    span_days = max((filters.end_date - filters.start_date).days + 1, 1)
    previous_end = filters.start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span_days - 1)
    return previous_start, previous_end


def _resolve_page_sections(
    engine: Engine,
    tasks: dict[str, Callable[[], Any]],
) -> dict[str, Any]:
    if engine.dialect.name == "sqlite" or len(tasks) <= 1:
        return {name: task() for name, task in tasks.items()}

    with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as executor:
        futures = {name: executor.submit(task) for name, task in tasks.items()}
        return {name: future.result() for name, future in futures.items()}
