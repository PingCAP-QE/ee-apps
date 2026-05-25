from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from cost_insight.common.config import GcpBillingSettings
from cost_insight.jobs import state_store

LOG = logging.getLogger(__name__)

JOB_NAME = "refresh_cost_attribution_daily"
SUMMARY_JOB_NAME = "refresh_cost_attribution_from_summary"
VENDOR = "gcp"


@dataclass(frozen=True)
class RefreshAttributionSummary:
    account_id: str
    start_date: date
    end_date: date
    rows_deleted: int
    rows_inserted: int
    dry_run: bool
    raw_rows: int | None = None
    summary_rows: int | None = None


def run_refresh_cost_attribution_daily(
    engine: Engine,
    *,
    settings: GcpBillingSettings,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> RefreshAttributionSummary:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")

    params = {
        "vendor": VENDOR,
        "account_id": settings.account_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    watermark = _watermark(account_id=settings.account_id, start_date=start_date, end_date=end_date)

    if dry_run:
        with engine.begin() as connection:
            raw_rows = connection.execute(_COUNT_RAW_DETAILS, params).scalar_one()
        return RefreshAttributionSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=0,
            rows_inserted=0,
            dry_run=True,
            raw_rows=int(raw_rows),
        )

    try:
        with engine.begin() as connection:
            state_store.mark_job_started(connection, JOB_NAME, watermark)

        with engine.begin() as connection:
            delete_result = connection.execute(_DELETE_ATTRIBUTION_DAILY, params)
            insert_result = connection.execute(_INSERT_ATTRIBUTION_DAILY, params)
            state_store.mark_job_succeeded(connection, JOB_NAME, watermark)

        return RefreshAttributionSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=_positive_rowcount(delete_result.rowcount),
            rows_inserted=_positive_rowcount(insert_result.rowcount),
            dry_run=False,
        )
    except Exception as exc:
        LOG.exception("refresh_cost_attribution_daily failed")
        with engine.begin() as connection:
            state_store.mark_job_failed(connection, JOB_NAME, watermark, repr(exc))
        raise


def run_refresh_cost_attribution_from_summary(
    engine: Engine,
    *,
    settings: GcpBillingSettings,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> RefreshAttributionSummary:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")

    params = {
        "vendor": VENDOR,
        "account_id": settings.account_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    watermark = _watermark(account_id=settings.account_id, start_date=start_date, end_date=end_date)

    if dry_run:
        with engine.begin() as connection:
            summary_rows = connection.execute(_COUNT_SUMMARY_DETAILS, params).scalar_one()
        return RefreshAttributionSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=0,
            rows_inserted=0,
            dry_run=True,
            summary_rows=int(summary_rows),
        )

    try:
        with engine.begin() as connection:
            state_store.mark_job_started(connection, SUMMARY_JOB_NAME, watermark)

        with engine.begin() as connection:
            delete_result = connection.execute(_DELETE_ATTRIBUTION_DAILY, params)
            insert_result = connection.execute(_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY, params)
            state_store.mark_job_succeeded(connection, SUMMARY_JOB_NAME, watermark)

        return RefreshAttributionSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=_positive_rowcount(delete_result.rowcount),
            rows_inserted=_positive_rowcount(insert_result.rowcount),
            dry_run=False,
        )
    except Exception as exc:
        LOG.exception("refresh_cost_attribution_from_summary failed")
        with engine.begin() as connection:
            state_store.mark_job_failed(connection, SUMMARY_JOB_NAME, watermark, repr(exc))
        raise


def _watermark(*, account_id: str, start_date: date, end_date: date) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def _positive_rowcount(rowcount: int | None) -> int:
    if rowcount is None or rowcount < 0:
        return 0
    return rowcount


def normalized_identity_sql(expression: str) -> str:
    base = f"SUBSTRING_INDEX(LOWER(COALESCE({expression}, '')), '@', 1)"
    return (
        "REPLACE("
        "REPLACE("
        f"REPLACE({base}, '-', '_'), "
        "'.', '_'), "
        "' ', '_')"
    )


_COUNT_RAW_DETAILS = text(
    """
    SELECT COUNT(*)
    FROM cost_raw_details
    WHERE usage_date BETWEEN :start_date AND :end_date
      AND vendor = :vendor
      AND account_id = :account_id
    """
)


_COUNT_SUMMARY_DETAILS = text(
    """
    SELECT COUNT(*)
    FROM cost_bq_export_summary_daily
    WHERE usage_date BETWEEN :start_date AND :end_date
      AND vendor = :vendor
      AND account_id = :account_id
    """
)


_DELETE_ATTRIBUTION_DAILY = text(
    """
    DELETE FROM cost_attribution_daily
    WHERE usage_date BETWEEN :start_date AND :end_date
      AND vendor = :vendor
      AND account_id = :account_id
    """
)


_NORMALIZED_RAW_AUTHOR = normalized_identity_sql("raw.author")
_NORMALIZED_SUMMARY_AUTHOR = normalized_identity_sql("summary.author")
_NORMALIZED_GITHUB_ID = normalized_identity_sql("normalized_employee.github_id")
_NORMALIZED_EMAIL_LOCAL = normalized_identity_sql(
    "SUBSTRING_INDEX(normalized_employee.email, '@', 1)"
)
_NORMALIZED_EN_NAME = normalized_identity_sql("normalized_employee.en_name")


_INSERT_ATTRIBUTION_DAILY = text(
    f"""
    INSERT INTO cost_attribution_daily (
      usage_date,
      vendor,
      account_id,
      service_name,
      sku_name,
      org,
      repo,
      resource_name,
      author,
      owner,
      attribution_key,
      attribution_source,
      attribution_status,
      employee_id,
      group_id,
      manager_id,
      usage_seconds,
      list_cost,
      effective_cost,
      credit_amount,
      net_cost,
      source_rows,
      dimension_hash
    )
    SELECT
      attributed.usage_date,
      attributed.vendor,
      attributed.account_id,
      attributed.service_name,
      attributed.sku_name,
      attributed.org,
      attributed.repo,
      attributed.resource_name,
      attributed.author,
      attributed.owner,
      attributed.attribution_key,
      attributed.attribution_source,
      attributed.attribution_status,
      attributed.employee_id,
      attributed.group_id,
      attributed.manager_id,
      SUM(attributed.usage_seconds) AS usage_seconds,
      SUM(attributed.list_cost) AS list_cost,
      SUM(attributed.effective_cost) AS effective_cost,
      SUM(attributed.credit_amount) AS credit_amount,
      SUM(attributed.net_cost) AS net_cost,
      COUNT(*) AS source_rows,
      SHA2(
        CONCAT_WS(
          '|',
          DATE_FORMAT(attributed.usage_date, '%Y-%m-%d'),
          COALESCE(attributed.vendor, ''),
          COALESCE(attributed.account_id, ''),
          COALESCE(attributed.service_name, ''),
          COALESCE(attributed.sku_name, ''),
          COALESCE(attributed.org, ''),
          COALESCE(attributed.repo, ''),
          COALESCE(attributed.resource_name, ''),
          COALESCE(attributed.author, ''),
          COALESCE(attributed.owner, ''),
          COALESCE(attributed.attribution_key, ''),
          COALESCE(attributed.attribution_source, ''),
          COALESCE(attributed.attribution_status, ''),
          COALESCE(CAST(attributed.employee_id AS CHAR), ''),
          COALESCE(CAST(attributed.group_id AS CHAR), ''),
          COALESCE(CAST(attributed.manager_id AS CHAR), '')
        ),
        256
      ) AS dimension_hash
    FROM (
      SELECT
        raw.usage_date,
        raw.vendor,
        raw.account_id,
        raw.service_name,
        raw.sku_name,
        raw.org,
        raw.repo,
        raw.resource_name,
        raw.author,
        raw.author AS owner,
        CASE
          WHEN COALESCE(
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN CONCAT(
            'employee:',
            CAST(COALESCE(
              github_employee.id,
              email_employee.id,
              normalized_employee.id
            ) AS CHAR)
          )
          WHEN raw.author IS NOT NULL THEN CONCAT('author:', LOWER(raw.author))
          ELSE 'unattributed'
        END AS attribution_key,
        CASE
          WHEN github_employee.id IS NOT NULL THEN 'author_github'
          WHEN email_employee.id IS NOT NULL THEN 'author_email'
          WHEN normalized_employee.id IS NOT NULL THEN 'author_normalized'
          WHEN raw.author IS NOT NULL THEN 'author_label'
          ELSE 'missing_author'
        END AS attribution_source,
        CASE
          WHEN COALESCE(
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN 'matched'
          WHEN raw.author IS NOT NULL THEN 'unmatched'
          ELSE 'unattributed'
        END AS attribution_status,
        COALESCE(
          github_employee.id,
          email_employee.id,
          normalized_employee.id
        ) AS employee_id,
        COALESCE(
          github_employee.group_id,
          email_employee.group_id,
          normalized_employee.group_id
        ) AS group_id,
        COALESCE(
          github_employee.manager_id,
          email_employee.manager_id,
          normalized_employee.manager_id,
          matched_group.manager_id
        ) AS manager_id,
        raw.usage_seconds,
        raw.list_cost,
        raw.effective_cost,
        raw.credit_amount,
        raw.net_cost
      FROM cost_raw_details raw
      LEFT JOIN roster_employees github_employee
        ON github_employee.is_active = 1
       AND raw.author IS NOT NULL
       AND github_employee.github_id IS NOT NULL
       AND LOWER(github_employee.github_id) = LOWER(raw.author)
      LEFT JOIN roster_employees email_employee
        ON github_employee.id IS NULL
       AND email_employee.is_active = 1
       AND raw.author IS NOT NULL
       AND email_employee.email IS NOT NULL
       AND (
         LOWER(email_employee.email) = LOWER(raw.author)
         OR LOWER(SUBSTRING_INDEX(email_employee.email, '@', 1)) = LOWER(raw.author)
       )
      LEFT JOIN roster_employees normalized_employee
        ON github_employee.id IS NULL
       AND email_employee.id IS NULL
       AND normalized_employee.is_active = 1
       AND raw.author IS NOT NULL
       AND (
         normalized_employee.github_id IS NOT NULL
         OR normalized_employee.email IS NOT NULL
         OR normalized_employee.en_name IS NOT NULL
       )
       AND (
         {_NORMALIZED_RAW_AUTHOR} = {_NORMALIZED_GITHUB_ID}
         OR {_NORMALIZED_RAW_AUTHOR} = {_NORMALIZED_EMAIL_LOCAL}
         OR {_NORMALIZED_RAW_AUTHOR} = {_NORMALIZED_EN_NAME}
       )
      LEFT JOIN roster_groups matched_group
        ON matched_group.is_active = 1
       AND matched_group.id = COALESCE(
         github_employee.group_id,
         email_employee.group_id,
         normalized_employee.group_id
       )
      WHERE raw.usage_date BETWEEN :start_date AND :end_date
        AND raw.vendor = :vendor
        AND raw.account_id = :account_id
    ) attributed
    GROUP BY
      attributed.usage_date,
      attributed.vendor,
      attributed.account_id,
      attributed.service_name,
      attributed.sku_name,
      attributed.org,
      attributed.repo,
      attributed.resource_name,
      attributed.author,
      attributed.owner,
      attributed.attribution_key,
      attributed.attribution_source,
      attributed.attribution_status,
      attributed.employee_id,
      attributed.group_id,
      attributed.manager_id
    """
)


_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY = text(
    f"""
    INSERT INTO cost_attribution_daily (
      usage_date,
      vendor,
      account_id,
      service_name,
      sku_name,
      org,
      repo,
      resource_name,
      author,
      owner,
      attribution_key,
      attribution_source,
      attribution_status,
      employee_id,
      group_id,
      manager_id,
      usage_seconds,
      list_cost,
      effective_cost,
      credit_amount,
      net_cost,
      source_rows,
      dimension_hash
    )
    SELECT
      attributed.usage_date,
      attributed.vendor,
      attributed.account_id,
      NULL AS service_name,
      NULL AS sku_name,
      attributed.org,
      attributed.repo,
      NULL AS resource_name,
      attributed.author,
      attributed.owner,
      attributed.attribution_key,
      attributed.attribution_source,
      attributed.attribution_status,
      attributed.employee_id,
      attributed.group_id,
      attributed.manager_id,
      NULL AS usage_seconds,
      SUM(attributed.list_cost) AS list_cost,
      SUM(attributed.effective_cost) AS effective_cost,
      SUM(attributed.credit_amount) AS credit_amount,
      SUM(attributed.net_cost) AS net_cost,
      COUNT(*) AS source_rows,
      SHA2(
        CONCAT_WS(
          '|',
          DATE_FORMAT(attributed.usage_date, '%Y-%m-%d'),
          COALESCE(attributed.vendor, ''),
          COALESCE(attributed.account_id, ''),
          '',
          '',
          COALESCE(attributed.org, ''),
          COALESCE(attributed.repo, ''),
          '',
          COALESCE(attributed.author, ''),
          COALESCE(attributed.owner, ''),
          COALESCE(attributed.attribution_key, ''),
          COALESCE(attributed.attribution_source, ''),
          COALESCE(attributed.attribution_status, ''),
          COALESCE(CAST(attributed.employee_id AS CHAR), ''),
          COALESCE(CAST(attributed.group_id AS CHAR), ''),
          COALESCE(CAST(attributed.manager_id AS CHAR), '')
        ),
        256
      ) AS dimension_hash
    FROM (
      SELECT
        summary.usage_date,
        summary.vendor,
        summary.account_id,
        summary.org,
        summary.repo,
        summary.author,
        summary.author AS owner,
        CASE
          WHEN COALESCE(
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN CONCAT(
            'employee:',
            CAST(COALESCE(
              github_employee.id,
              email_employee.id,
              normalized_employee.id
            ) AS CHAR)
          )
          WHEN summary.author IS NOT NULL THEN CONCAT('author:', LOWER(summary.author))
          ELSE 'unattributed'
        END AS attribution_key,
        CASE
          WHEN github_employee.id IS NOT NULL THEN 'author_github'
          WHEN email_employee.id IS NOT NULL THEN 'author_email'
          WHEN normalized_employee.id IS NOT NULL THEN 'author_normalized'
          WHEN summary.author IS NOT NULL THEN 'author_label'
          ELSE 'missing_author'
        END AS attribution_source,
        CASE
          WHEN COALESCE(
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN 'matched'
          WHEN summary.author IS NOT NULL THEN 'unmatched'
          ELSE 'unattributed'
        END AS attribution_status,
        COALESCE(
          github_employee.id,
          email_employee.id,
          normalized_employee.id
        ) AS employee_id,
        COALESCE(
          github_employee.group_id,
          email_employee.group_id,
          normalized_employee.group_id
        ) AS group_id,
        COALESCE(
          github_employee.manager_id,
          email_employee.manager_id,
          normalized_employee.manager_id,
          matched_group.manager_id
        ) AS manager_id,
        summary.list_cost,
        summary.effective_cost,
        summary.credit_amount,
        summary.net_cost
      FROM cost_bq_export_summary_daily summary
      LEFT JOIN roster_employees github_employee
        ON github_employee.is_active = 1
       AND summary.author IS NOT NULL
       AND github_employee.github_id IS NOT NULL
       AND LOWER(github_employee.github_id) = LOWER(summary.author)
      LEFT JOIN roster_employees email_employee
        ON github_employee.id IS NULL
       AND email_employee.is_active = 1
       AND summary.author IS NOT NULL
       AND email_employee.email IS NOT NULL
       AND (
         LOWER(email_employee.email) = LOWER(summary.author)
         OR LOWER(SUBSTRING_INDEX(email_employee.email, '@', 1)) = LOWER(summary.author)
       )
      LEFT JOIN roster_employees normalized_employee
        ON github_employee.id IS NULL
       AND email_employee.id IS NULL
       AND normalized_employee.is_active = 1
       AND summary.author IS NOT NULL
       AND (
         normalized_employee.github_id IS NOT NULL
         OR normalized_employee.email IS NOT NULL
         OR normalized_employee.en_name IS NOT NULL
       )
       AND (
         {_NORMALIZED_SUMMARY_AUTHOR} = {_NORMALIZED_GITHUB_ID}
         OR {_NORMALIZED_SUMMARY_AUTHOR} = {_NORMALIZED_EMAIL_LOCAL}
         OR {_NORMALIZED_SUMMARY_AUTHOR} = {_NORMALIZED_EN_NAME}
       )
      LEFT JOIN roster_groups matched_group
        ON matched_group.is_active = 1
       AND matched_group.id = COALESCE(
         github_employee.group_id,
         email_employee.group_id,
         normalized_employee.group_id
       )
      WHERE summary.usage_date BETWEEN :start_date AND :end_date
        AND summary.vendor = :vendor
        AND summary.account_id = :account_id
    ) attributed
    GROUP BY
      attributed.usage_date,
      attributed.vendor,
      attributed.account_id,
      attributed.org,
      attributed.repo,
      attributed.author,
      attributed.owner,
      attributed.attribution_key,
      attributed.attribution_source,
      attributed.attribution_status,
      attributed.employee_id,
      attributed.group_id,
      attributed.manager_id
    """
)
