from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from cost_insight.common.config import AwsBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled, upsert_cost_source
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.materialize_resource_serving import run_materialize_resource_serving
from cost_insight.jobs.sync_gcp_unmatched_resources import (
    SyncGcpUnmatchedResourcesSummary,
    _normalize_resource_row,
    _watermark as gcp_watermark,
    replace_unmatched_resource_usage_dates,
    write_unmatched_resource_rows,
)
from cost_insight.jobs.sync_gcp_billing_summary import _dump_spooled_row, _iter_spooled_rows
from cost_insight.sources.aws_billing_export import fetch_aws_unmatched_resource_rows
from cost_insight.sources.aws_split_cost_export import fetch_aws_split_cost_unmatched_resource_rows
from cost_insight.jobs.sync_aws_billing_summary import (
    AWS_CUR_LEGACY_SCHEMA_VERSION,
    AWS_SPLIT_COST_SCHEMA_VERSION,
    AwsBillingSource,
)

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
    replace_existing_usage_dates: bool = False,
    validate_guardrail: bool = True,
    source: AwsBillingSource | None = None,
    fetch_rows: RowFetcher | None = None,
) -> SyncAwsUnmatchedResourcesSummary:
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    resolved_source = source or AwsBillingSource(
        account_id=account_id,
        billing_table=settings.billing_table,
    )
    if resolved_source.account_id != account_id:
        raise ValueError("AWS source account_id must match the requested account_id")
    if resolved_source.schema_version not in {
        AWS_CUR_LEGACY_SCHEMA_VERSION,
        AWS_SPLIT_COST_SCHEMA_VERSION,
    }:
        raise ValueError(f"Unsupported AWS source schema: {resolved_source.schema_version!r}")
    if replace_existing_usage_dates and resolved_source.schema_version != AWS_SPLIT_COST_SCHEMA_VERSION:
        raise ValueError("usage-date replacement is only supported for AWS split-cost sources")
    if (
        resolved_source.available_from is not None
        and usage_start_date < resolved_source.available_from
    ):
        raise ValueError("usage_start_date is before the AWS source availability date")
    resolved_fetch_rows = fetch_rows or _default_fetch_rows(resolved_source.schema_version)
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
        if replace_existing_usage_dates:
            with tempfile.TemporaryFile("w+b") as row_spool:
                for source_row in _fetch_source_rows(
                    fetch_rows=resolved_fetch_rows,
                    source=resolved_source,
                    account_id=account_id,
                    export_partition_start=resolved_export_start,
                    export_partition_end=resolved_export_end,
                    usage_start_date=usage_start_date,
                    usage_end_date=usage_end_date,
                    page_size=settings.page_size,
                    limit=limit,
                    validate_guardrail=validate_guardrail,
                ):
                    rows_seen += 1
                    normalized = _normalize_resource_row(source_row)
                    source_billing_account_id = source_billing_account_id or normalized[
                        "billing_account_id"
                    ]
                    _dump_spooled_row(row_spool, normalized)
                if rows_seen == 0:
                    raise ValueError("usage-date replacement source returned no rows")
                rows_written += replace_unmatched_resource_usage_dates(
                    engine,
                    _iter_spooled_rows(row_spool),
                    row_count=rows_seen,
                    vendor="aws",
                    account_id=account_id,
                    usage_start_date=usage_start_date,
                    usage_end_date=usage_end_date,
                    dry_run=dry_run,
                    batch_size=settings.page_size,
                )
        else:
            batch: list[dict[str, Any]] = []
            for source_row in _fetch_source_rows(
                fetch_rows=resolved_fetch_rows,
                source=resolved_source,
                account_id=account_id,
                export_partition_start=resolved_export_start,
                export_partition_end=resolved_export_end,
                usage_start_date=usage_start_date,
                usage_end_date=usage_end_date,
                page_size=settings.page_size,
                limit=limit,
                validate_guardrail=validate_guardrail,
            ):
                rows_seen += 1
                normalized = _normalize_resource_row(source_row)
                source_billing_account_id = source_billing_account_id or normalized[
                    "billing_account_id"
                ]
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

            run_materialize_resource_serving(
                engine,
                start_date=usage_start_date,
                end_date=usage_end_date,
                vendor="aws",
                account_id=account_id,
            )

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


def _default_fetch_rows(schema_version: str) -> RowFetcher:
    if schema_version == AWS_SPLIT_COST_SCHEMA_VERSION:
        return fetch_aws_split_cost_unmatched_resource_rows
    return fetch_aws_unmatched_resource_rows


def _fetch_source_rows(
    *,
    fetch_rows: RowFetcher,
    source: AwsBillingSource,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    usage_start_date: date,
    usage_end_date: date,
    page_size: int,
    limit: int | None,
    validate_guardrail: bool,
) -> Iterable[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "billing_table": source.billing_table,
        "account_id": account_id,
        "export_partition_start": export_partition_start,
        "export_partition_end": export_partition_end,
        "usage_start_date": usage_start_date,
        "usage_end_date": usage_end_date,
        "page_size": page_size,
        "limit": limit,
    }
    if source.schema_version == AWS_SPLIT_COST_SCHEMA_VERSION:
        kwargs["validate_guardrail"] = validate_guardrail
    return fetch_rows(**kwargs)


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
