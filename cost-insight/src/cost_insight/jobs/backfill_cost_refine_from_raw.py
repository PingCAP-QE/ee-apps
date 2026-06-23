from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.row_utils import coerce_date
from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_billing_summary import (
    JOB_NAME as SUMMARY_JOB_NAME,
)
from cost_insight.jobs.sync_gcp_billing_summary import (
    _normalize_summary_row,
    _watermark as summary_watermark,
    write_summary_rows,
)
from cost_insight.jobs.sync_gcp_unmatched_resources import (
    _normalize_resource_row,
    write_unmatched_resource_rows,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "backfill_cost_refine_from_raw"


@dataclass(frozen=True)
class BackfillCostRefineFromRawSummary:
    account_id: str
    start_date: date
    end_date: date
    summary_rows_seen: int
    summary_rows_written: int
    unmatched_rows_seen: int
    unmatched_rows_written: int
    export_partition_start: date | None
    export_partition_end: date | None
    dry_run: bool
    marked_summary_watermark: bool


def run_backfill_cost_refine_from_raw(
    engine: Engine,
    *,
    settings: GcpBillingSettings,
    start_date: date,
    end_date: date,
    include_unmatched_resources: bool = True,
    mark_summary_watermark: bool = False,
    dry_run: bool = False,
) -> BackfillCostRefineFromRawSummary:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")

    job_name = source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id)
    params = {
        "vendor": "gcp",
        "account_id": settings.account_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    watermark = {
        "account_id": settings.account_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }

    if not dry_run:
        with engine.begin() as connection:
            state_store.mark_job_started(connection, job_name, watermark)

    try:
        summary_rows_seen = 0
        summary_rows_written = 0
        unmatched_rows_seen = 0
        unmatched_rows_written = 0

        with engine.connect() as connection:
            export_range = connection.execute(_SELECT_SYNTHETIC_EXPORT_RANGE, params).mappings().one()

        export_partition_start = coerce_date(export_range["export_partition_start"])
        export_partition_end = coerce_date(export_range["export_partition_end"])

        with engine.connect() as connection:
            summary_batch = []
            for row in connection.execute(_SELECT_SUMMARY_ROWS, params).mappings():
                summary_rows_seen += 1
                summary_batch.append(_normalize_summary_row(dict(row)))
                if len(summary_batch) >= settings.page_size:
                    summary_rows_written += write_summary_rows(
                        engine,
                        summary_batch,
                        dry_run=dry_run,
                    )
                    summary_batch.clear()
            summary_rows_written += write_summary_rows(engine, summary_batch, dry_run=dry_run)

        if include_unmatched_resources:
            with engine.connect() as connection:
                unmatched_batch = []
                for row in connection.execute(_SELECT_UNMATCHED_RESOURCE_ROWS, params).mappings():
                    unmatched_rows_seen += 1
                    unmatched_batch.append(_normalize_resource_row(dict(row)))
                    if len(unmatched_batch) >= settings.page_size:
                        unmatched_rows_written += write_unmatched_resource_rows(
                            engine,
                            unmatched_batch,
                            dry_run=dry_run,
                        )
                        unmatched_batch.clear()
                unmatched_rows_written += write_unmatched_resource_rows(
                    engine,
                    unmatched_batch,
                    dry_run=dry_run,
                )

        marked_summary_watermark = False
        if (
            mark_summary_watermark
            and not dry_run
            and export_partition_start is not None
            and export_partition_end is not None
        ):
            with engine.begin() as connection:
                state_store.mark_job_succeeded(
                    connection,
                    source_job_name(
                        SUMMARY_JOB_NAME,
                        vendor="gcp",
                        account_id=settings.account_id,
                    ),
                    summary_watermark(
                        account_id=settings.account_id,
                        export_partition_start=export_partition_start,
                        export_partition_end=export_partition_end,
                    ),
                )
            marked_summary_watermark = True

        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_succeeded(connection, job_name, watermark)

        return BackfillCostRefineFromRawSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            summary_rows_seen=summary_rows_seen,
            summary_rows_written=summary_rows_written,
            unmatched_rows_seen=unmatched_rows_seen,
            unmatched_rows_written=unmatched_rows_written,
            export_partition_start=export_partition_start,
            export_partition_end=export_partition_end,
            dry_run=dry_run,
            marked_summary_watermark=marked_summary_watermark,
        )
    except Exception as exc:
        LOG.exception("backfill_cost_refine_from_raw failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


_SYNTHETIC_EXPORT_PARTITION_DATE = "COALESCE(DATE(source_export_time), usage_date)"

_SELECT_SYNTHETIC_EXPORT_RANGE = text(
    f"""
    SELECT
      MIN({_SYNTHETIC_EXPORT_PARTITION_DATE}) AS export_partition_start,
      MAX({_SYNTHETIC_EXPORT_PARTITION_DATE}) AS export_partition_end
    FROM cost_raw_details
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND usage_date BETWEEN :start_date AND :end_date
    """
)

_SELECT_SUMMARY_ROWS = text(
    f"""
    SELECT
      vendor,
      account_id,
      billing_account_id,
      {_SYNTHETIC_EXPORT_PARTITION_DATE} AS export_partition_date,
      usage_date,
      service_name,
      sku_name,
      author,
      org,
      repo,
      target_branch,
      ROUND(SUM(list_cost), 2) AS list_cost,
      ROUND(SUM(effective_cost), 2) AS effective_cost,
      ROUND(SUM(credit_amount), 2) AS credit_amount,
      ROUND(SUM(net_cost), 2) AS net_cost,
      MAX(source_export_time) AS source_export_time
    FROM cost_raw_details
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND usage_date BETWEEN :start_date AND :end_date
    GROUP BY
      vendor,
      account_id,
      billing_account_id,
      export_partition_date,
      usage_date,
      service_name,
      sku_name,
      author,
      org,
      repo,
      target_branch
    ORDER BY export_partition_date, usage_date, service_name, sku_name, author, org, repo, target_branch
    """
)

_SELECT_UNMATCHED_RESOURCE_ROWS = text(
    f"""
    SELECT
      vendor,
      account_id,
      billing_account_id,
      {_SYNTHETIC_EXPORT_PARTITION_DATE} AS export_partition_date,
      usage_date,
      service_name,
      sku_name,
      namespace,
      author,
      org,
      repo,
      target_branch,
      resource_name,
      ROUND(SUM(usage_seconds), 2) AS usage_seconds,
      ROUND(SUM(list_cost), 2) AS list_cost,
      ROUND(SUM(effective_cost), 2) AS effective_cost,
      ROUND(SUM(credit_amount), 2) AS credit_amount,
      ROUND(SUM(net_cost), 2) AS net_cost,
      MAX(source_export_time) AS source_export_time
    FROM cost_raw_details
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND usage_date BETWEEN :start_date AND :end_date
      AND resource_name IS NOT NULL
      AND resource_name <> ''
    GROUP BY
      vendor,
      account_id,
      billing_account_id,
      export_partition_date,
      usage_date,
      service_name,
      sku_name,
      namespace,
      author,
      org,
      repo,
      target_branch,
      resource_name
    ORDER BY usage_date, service_name, sku_name, resource_name
    """
)
