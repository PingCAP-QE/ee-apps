from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from cost_insight.common.config import AlibabaBillingSettings
from cost_insight.jobs.sync_gcp_billing_summary import (
    SyncGcpBillingSummaryResult,
    run_sync_billing_summary,
)
from cost_insight.sources.alibaba_billing_export import fetch_alibaba_billing_summary_rows

JOB_NAME = "sync_alibaba_billing_summary"
ALIBABA_ACCOUNT_DISPLAY_NAMES = {"5028760335873601": "alicloud-testing-infra-dev"}
RowFetcher = Callable[..., Iterable[dict[str, Any]]]


def run_sync_alibaba_billing_summary(
    engine: Engine,
    *,
    settings: AlibabaBillingSettings,
    account_id: str,
    display_name: str,
    export_partition_start: date | None = None,
    export_partition_end: date | None = None,
    earliest_usage_date: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    replace_existing_partitions: bool = False,
    replacement_usage_start_date: date | None = None,
    replacement_usage_end_date: date | None = None,
    fetch_rows: RowFetcher = fetch_alibaba_billing_summary_rows,
) -> SyncGcpBillingSummaryResult:
    return run_sync_billing_summary(
        engine,
        settings=replace(settings, account_id=account_id),
        vendor="alibaba",
        job_name=JOB_NAME,
        display_name=display_name,
        export_partition_start=export_partition_start,
        export_partition_end=export_partition_end,
        earliest_usage_date=earliest_usage_date,
        dry_run=dry_run,
        limit=limit,
        replace_existing_partitions=replace_existing_partitions,
        replacement_usage_start_date=replacement_usage_start_date,
        replacement_usage_end_date=replacement_usage_end_date,
        fetch_rows=fetch_rows,
    )
