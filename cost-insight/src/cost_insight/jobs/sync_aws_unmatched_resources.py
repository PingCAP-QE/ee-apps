from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from cost_insight.common.config import AwsBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled, upsert_cost_source
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_unmatched_resources import (
    SyncGcpUnmatchedResourcesSummary,
    _normalize_resource_row,
    _watermark as gcp_watermark,
    write_unmatched_resource_rows,
)
from cost_insight.sources.aws_billing_export import fetch_aws_unmatched_resource_rows

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_aws_unmatched_resources"
RowFetcher = Callable[..., Iterable[dict[str, Any]]]


@dataclass(frozen=True)
class SyncAwsUnmatchedResourcesSummary(SyncGcpUnmatchedResourcesSummary):
    pass


def run_sync_aws_unmatched_resources(
    engine: Engine,
    *,
    settings: AwsBillingSettings,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    export_partition_start: date | None = None,
    export_partition_end: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    fetch_rows: RowFetcher = fetch_aws_unmatched_resource_rows,
) -> SyncAwsUnmatchedResourcesSummary:
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    job_name = source_job_name(JOB_NAME, vendor="aws", account_id=account_id)
    # AWS staging data is partitioned by billing month, so the default partition
    # window must expand a mid-month usage request to the first day of that month.
    resolved_export_start = export_partition_start or usage_start_date.replace(day=1)
    resolved_export_end = export_partition_end or usage_end_date.replace(day=1)
    watermark = _watermark(
        account_id=account_id,
        usage_start_date=usage_start_date,
        usage_end_date=usage_end_date,
        export_partition_start=resolved_export_start,
        export_partition_end=resolved_export_end,
    )
    with engine.begin() as connection:
        ensure_cost_source_enabled(
            connection,
            vendor="aws",
            account_id=account_id,
            dry_run=dry_run,
            display_name=account_id,
        )
        if not dry_run:
            state_store.mark_job_started(connection, job_name, watermark)

    try:
        rows_seen = 0
        rows_written = 0
        source_billing_account_id: str | None = None
        batch: list[dict[str, Any]] = []
        for source_row in fetch_rows(
            billing_table=settings.billing_table,
            account_id=account_id,
            export_partition_start=resolved_export_start,
            export_partition_end=resolved_export_end,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            page_size=settings.page_size,
            limit=limit,
        ):
            rows_seen += 1
            normalized = _normalize_resource_row(source_row)
            source_billing_account_id = source_billing_account_id or normalized["billing_account_id"]
            batch.append(normalized)
            if len(batch) >= settings.page_size:
                rows_written += write_unmatched_resource_rows(engine, batch, dry_run=dry_run)
                batch.clear()
        rows_written += write_unmatched_resource_rows(engine, batch, dry_run=dry_run)

        if not dry_run:
            with engine.begin() as connection:
                if source_billing_account_id:
                    upsert_cost_source(
                        connection,
                        vendor="aws",
                        account_id=account_id,
                        billing_account_id=str(source_billing_account_id),
                        display_name=account_id,
                    )
                state_store.mark_job_succeeded(connection, job_name, watermark)

        return SyncAwsUnmatchedResourcesSummary(
            account_id=account_id,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            export_partition_start=resolved_export_start,
            export_partition_end=resolved_export_end,
            rows_seen=rows_seen,
            rows_written=rows_written,
            dry_run=dry_run,
        )
    except Exception as exc:
        LOG.exception("sync_aws_unmatched_resources failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def _watermark(
    *,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    export_partition_start: date,
    export_partition_end: date,
) -> dict[str, Any]:
    payload = gcp_watermark(
        account_id=account_id,
        usage_start_date=usage_start_date,
        usage_end_date=usage_end_date,
        export_partition_start=export_partition_start,
        export_partition_end=export_partition_end,
    )
    payload["vendor"] = "aws"
    return payload
