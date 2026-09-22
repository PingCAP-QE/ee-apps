from __future__ import annotations

from datetime import date

from fastapi import HTTPException, Query

from ci_dashboard.api.queries.base import CommonFilters, SUPPORTED_GRANULARITIES, split_filter_values


def get_common_filters(
    repo: str | None = None,
    branch: str | None = None,
    job_name: str | None = None,
    cloud_phase: str | None = None,
    issue_status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    granularity: str = Query(default="day"),
    cost_source: str | None = None,
    owner_include: str | None = None,
    owner_exclude: str | None = None,
    team_include: str | None = None,
    team_exclude: str | None = None,
    project_include: str | None = None,
    project_exclude: str | None = None,
) -> CommonFilters:
    validate_granularity(granularity)
    validate_date_range(start_date, end_date)
    validate_issue_status(issue_status)
    cost_sources = parse_cost_sources(cost_source)
    cost_vendor, cost_account_id = cost_sources[0] if len(cost_sources) == 1 else (None, None)
    return CommonFilters(
        repo=repo,
        branch=branch,
        job_name=job_name,
        cloud_phase=cloud_phase,
        issue_status=issue_status,
        start_date=start_date,
        end_date=end_date,
        granularity=granularity,
        cost_vendor=cost_vendor,
        cost_account_id=cost_account_id,
        cost_sources=cost_sources,
        owner_include=split_filter_values(owner_include),
        owner_exclude=split_filter_values(owner_exclude),
        team_include=split_filter_values(team_include),
        team_exclude=split_filter_values(team_exclude),
        project_include=split_filter_values(project_include),
        project_exclude=split_filter_values(project_exclude),
    )


def validate_granularity(granularity: str) -> None:
    if granularity not in SUPPORTED_GRANULARITIES:
        raise HTTPException(status_code=400, detail="granularity must be one of: day, week, month")


def validate_date_range(start_date: date | None, end_date: date | None) -> None:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")


def validate_issue_status(issue_status: str | None) -> None:
    if issue_status is None or issue_status == "":
        return
    if issue_status not in {"open", "closed"}:
        raise HTTPException(status_code=400, detail="issue_status must be one of: open, closed")


def parse_cost_sources(cost_source: str | None) -> tuple[tuple[str, str], ...]:
    if cost_source is None or not cost_source.strip() or cost_source.strip() == "all":
        return ()

    sources: list[tuple[str, str]] = []
    for value in cost_source.split(","):
        item = value.strip()
        if not item or item == "all":
            raise HTTPException(
                status_code=400,
                detail="cost_source must be 'all' or comma-separated '<vendor>:<account_id>' values",
            )
        vendor, separator, account_id = item.partition(":")
        if not separator or not vendor or not account_id:
            raise HTTPException(
                status_code=400,
                detail="cost_source must be 'all' or comma-separated '<vendor>:<account_id>' values",
            )
        source = (vendor, account_id)
        if source not in sources:
            sources.append(source)
    return tuple(sources)
