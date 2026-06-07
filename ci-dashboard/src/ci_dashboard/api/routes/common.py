from __future__ import annotations

from datetime import date

from fastapi import HTTPException, Query

from ci_dashboard.api.queries.base import CommonFilters, SUPPORTED_GRANULARITIES


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
) -> CommonFilters:
    validate_granularity(granularity)
    validate_date_range(start_date, end_date)
    validate_issue_status(issue_status)
    cost_vendor, cost_account_id = parse_cost_source(cost_source)
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


def parse_cost_source(cost_source: str | None) -> tuple[str | None, str | None]:
    if cost_source is None or cost_source == "" or cost_source == "all":
        return None, None

    vendor, separator, account_id = cost_source.partition(":")
    if not separator or not vendor or not account_id:
        raise HTTPException(
            status_code=400,
            detail="cost_source must be 'all' or formatted as '<vendor>:<account_id>'",
        )
    return vendor, account_id
