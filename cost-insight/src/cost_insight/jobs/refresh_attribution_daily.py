from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs import state_store

LOG = logging.getLogger(__name__)

JOB_NAME = "refresh_cost_attribution_daily"
SUMMARY_JOB_NAME = "refresh_cost_attribution_from_summary"


@dataclass(frozen=True)
class CostAttributionSource:
    vendor: str
    account_id: str


@dataclass(frozen=True)
class RefreshAttributionSummary:
    vendor: str
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
    source: CostAttributionSource,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> RefreshAttributionSummary:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")

    params = {
        "vendor": source.vendor,
        "account_id": source.account_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    watermark = _watermark(vendor=source.vendor, account_id=source.account_id, start_date=start_date, end_date=end_date)
    job_name = source_job_name(JOB_NAME, vendor=source.vendor, account_id=source.account_id)

    if dry_run:
        with engine.begin() as connection:
            raw_rows = connection.execute(_COUNT_RAW_DETAILS, params).scalar_one()
        return RefreshAttributionSummary(
            vendor=source.vendor,
            account_id=source.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=0,
            rows_inserted=0,
            dry_run=True,
            raw_rows=int(raw_rows),
        )

    try:
        with engine.begin() as connection:
            state_store.mark_job_started(connection, job_name, watermark)

        with engine.begin() as connection:
            delete_result = connection.execute(_DELETE_ATTRIBUTION_DAILY, params)
            insert_result = connection.execute(_INSERT_ATTRIBUTION_DAILY, params)
            state_store.mark_job_succeeded(connection, job_name, watermark)

        return RefreshAttributionSummary(
            vendor=source.vendor,
            account_id=source.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=_positive_rowcount(delete_result.rowcount),
            rows_inserted=_positive_rowcount(insert_result.rowcount),
            dry_run=False,
        )
    except Exception as exc:
        LOG.exception("refresh_cost_attribution_daily failed")
        with engine.begin() as connection:
            state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def run_refresh_cost_attribution_from_summary(
    engine: Engine,
    *,
    source: CostAttributionSource,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
    tcms_allocation_table: str | None = None,
) -> RefreshAttributionSummary:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")

    params = {
        "vendor": source.vendor,
        "account_id": source.account_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    watermark = _watermark(vendor=source.vendor, account_id=source.account_id, start_date=start_date, end_date=end_date)
    job_name = source_job_name(SUMMARY_JOB_NAME, vendor=source.vendor, account_id=source.account_id)

    if dry_run:
        with engine.begin() as connection:
            summary_rows = connection.execute(_COUNT_SUMMARY_DETAILS, params).scalar_one()
        return RefreshAttributionSummary(
            vendor=source.vendor,
            account_id=source.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=0,
            rows_inserted=0,
            dry_run=True,
            summary_rows=int(summary_rows),
        )

    try:
        with engine.begin() as connection:
            state_store.mark_job_started(connection, job_name, watermark)

        with engine.begin() as connection:
            delete_result = connection.execute(_DELETE_ATTRIBUTION_DAILY, params)
            rows_inserted = 0
            for insert_statement in _summary_insert_statements(
                source=source,
                tcms_allocation_table=tcms_allocation_table,
            ):
                insert_result = connection.execute(insert_statement, params)
                rows_inserted += _positive_rowcount(insert_result.rowcount)
            state_store.mark_job_succeeded(connection, job_name, watermark)

        return RefreshAttributionSummary(
            vendor=source.vendor,
            account_id=source.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=_positive_rowcount(delete_result.rowcount),
            rows_inserted=rows_inserted,
            dry_run=False,
        )
    except Exception as exc:
        LOG.exception("refresh_cost_attribution_from_summary failed")
        with engine.begin() as connection:
            state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def _watermark(*, vendor: str, account_id: str, start_date: date, end_date: date) -> dict[str, Any]:
    return {
        "vendor": vendor,
        "account_id": account_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def _positive_rowcount(rowcount: int | None) -> int:
    if rowcount is None or rowcount < 0:
        return 0
    return rowcount


def _summary_insert_statements(
    *,
    source: CostAttributionSource,
    tcms_allocation_table: str | None,
):
    if source.vendor != "aws" or not tcms_allocation_table:
        return (_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY,)
    quoted_tcms_table = _quote_table_identifier(tcms_allocation_table)
    return (
        _build_insert_attribution_daily_from_summary_with_tcms(quoted_tcms_table),
        _build_insert_shared_attribution_daily_from_summary(quoted_tcms_table),
    )


def _quote_table_identifier(table_name: str) -> str:
    parts = table_name.split(".")
    if len(parts) not in {1, 2}:
        raise ValueError("tcms allocation table must be table or database.table")
    for part in parts:
        if not part or not part.replace("_", "").isalnum() or part[0].isdigit():
            raise ValueError(f"Invalid tcms allocation table identifier: {table_name!r}")
    return ".".join(f"`{part}`" for part in parts)


def normalized_identity_sql(expression: str) -> str:
    base = f"SUBSTRING_INDEX(LOWER(COALESCE({expression}, '')), '@', 1)"
    return (
        "REPLACE("
        "REPLACE("
        "REPLACE("
        f"REPLACE({base}, '-', ''), "
        "'.', ''), "
        "'_', ''), "
        "' ', '')"
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
_RAW_AUTHOR_OVERRIDE_EMAIL = """
CASE LOWER(raw.author)
  WHEN 'flaky-claw' THEN 'yinsu@pingcap.com'
  WHEN 'ti-chi-bot' THEN 'wei.zheng@pingcap.com'
  ELSE NULL
END
""".strip()
_SUMMARY_AUTHOR_OVERRIDE_EMAIL = """
CASE LOWER(summary.author)
  WHEN 'flaky-claw' THEN 'yinsu@pingcap.com'
  WHEN 'ti-chi-bot' THEN 'wei.zheng@pingcap.com'
  ELSE NULL
END
""".strip()
_BASE_IDENTITY_OVERRIDE_EMAIL = """
CASE LOWER(base.match_identity)
  WHEN 'flaky-claw' THEN 'yinsu@pingcap.com'
  WHEN 'ti-chi-bot' THEN 'wei.zheng@pingcap.com'
  ELSE NULL
END
""".strip()
_NORMALIZED_BASE_IDENTITY = normalized_identity_sql("base.match_identity")


def _json_tag_value_sql(expression: str, key: str) -> str:
    return f"JSON_UNQUOTE(JSON_EXTRACT({expression}, '$.{key}'))"


def _json_tag_is_json_null_sql(expression: str, key: str) -> str:
    return f"UPPER(JSON_TYPE(JSON_EXTRACT({expression}, '$.{key}'))) = 'NULL'"


def _allocation_tags_for_match_sql(expression: str) -> str:
    cluster_is_null = _json_tag_is_json_null_sql(expression, "cluster")
    shared_pool_is_null = _json_tag_is_json_null_sql(expression, "shared_pool")
    return f"""
CASE
  WHEN {cluster_is_null} AND {shared_pool_is_null}
    THEN JSON_REMOVE({expression}, '$.cluster', '$.shared_pool')
  WHEN {cluster_is_null}
    THEN JSON_REMOVE({expression}, '$.cluster')
  WHEN {shared_pool_is_null}
    THEN JSON_REMOVE({expression}, '$.shared_pool')
  ELSE {expression}
END
""".strip()


_SUMMARY_SHARED_POOL = _json_tag_value_sql("summary.vendor_tags_json", "shared_pool")
_SUMMARY_CLUSTER = _json_tag_value_sql("summary.vendor_tags_json", "cluster")
_ALLOCATION_MATCH_CLUSTER = _json_tag_value_sql(
    "allocation.match_tags_json", "cluster"
)
_ALLOCATION_MATCH_TAGS_JSON = _allocation_tags_for_match_sql(
    "allocation_raw.vendor_tags_json"
)
_MATCHED_OWNER = """
COALESCE(
  override_employee.email,
  github_employee.email,
  email_employee.email,
  normalized_employee.email,
  override_employee.en_name,
  github_employee.en_name,
  email_employee.en_name,
  normalized_employee.en_name
)
""".strip()
_TCMS_OWNER = f"""
CASE
  WHEN base.identity_kind = 'owner_email' THEN base.match_identity
  ELSE {_MATCHED_OWNER}
END
""".strip()


_INSERT_ATTRIBUTION_DAILY = text(
    f"""
    INSERT INTO cost_attribution_daily (
      usage_date,
      vendor,
      account_id,
      service_name,
      sku_name,
      region,
      org,
      repo,
      target_branch,
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
      attributed.region,
      attributed.org,
      attributed.repo,
      attributed.target_branch,
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
          COALESCE(attributed.region, ''),
          COALESCE(attributed.org, ''),
          COALESCE(attributed.repo, ''),
          COALESCE(attributed.target_branch, ''),
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
        raw.region,
        raw.org,
        raw.repo,
        raw.target_branch,
        raw.resource_name,
        raw.author,
        {_MATCHED_OWNER} AS owner,
        CASE
          WHEN COALESCE(
            override_employee.id,
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN CONCAT(
            'employee:',
            CAST(COALESCE(
              override_employee.id,
              github_employee.id,
              email_employee.id,
              normalized_employee.id
            ) AS CHAR)
          )
          WHEN raw.author IS NOT NULL THEN CONCAT('author:', LOWER(raw.author))
          ELSE 'unattributed'
        END AS attribution_key,
        CASE
          WHEN override_employee.id IS NOT NULL THEN 'author_override'
          WHEN github_employee.id IS NOT NULL THEN 'author_github'
          WHEN email_employee.id IS NOT NULL THEN 'author_email'
          WHEN normalized_employee.id IS NOT NULL THEN 'author_normalized'
          WHEN raw.author IS NOT NULL THEN 'author_label'
          ELSE 'missing_author'
        END AS attribution_source,
        CASE
          WHEN COALESCE(
            override_employee.id,
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN 'matched'
          WHEN raw.author IS NOT NULL THEN 'unmatched'
          ELSE 'unattributed'
        END AS attribution_status,
        COALESCE(
          override_employee.id,
          github_employee.id,
          email_employee.id,
          normalized_employee.id
        ) AS employee_id,
        COALESCE(
          override_employee.group_id,
          github_employee.group_id,
          email_employee.group_id,
          normalized_employee.group_id
        ) AS group_id,
        COALESCE(
          override_employee.manager_id,
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
      LEFT JOIN roster_employees override_employee
        ON raw.author IS NOT NULL
       AND override_employee.email IS NOT NULL
       AND LOWER(override_employee.email) = LOWER({_RAW_AUTHOR_OVERRIDE_EMAIL})
      LEFT JOIN roster_employees github_employee
        ON override_employee.id IS NULL
       AND raw.author IS NOT NULL
       AND github_employee.github_id IS NOT NULL
       AND LOWER(github_employee.github_id) = LOWER(raw.author)
      LEFT JOIN roster_employees email_employee
        ON github_employee.id IS NULL
       AND raw.author IS NOT NULL
       AND email_employee.email IS NOT NULL
       AND (
         LOWER(email_employee.email) = LOWER(raw.author)
         OR LOWER(SUBSTRING_INDEX(email_employee.email, '@', 1)) = LOWER(raw.author)
       )
      LEFT JOIN roster_employees normalized_employee
        ON github_employee.id IS NULL
       AND email_employee.id IS NULL
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
         override_employee.group_id,
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
      attributed.region,
      attributed.org,
      attributed.repo,
      attributed.target_branch,
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
      region,
      org,
      repo,
      target_branch,
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
      attributed.region,
      attributed.org,
      attributed.repo,
      attributed.target_branch,
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
          COALESCE(attributed.service_name, ''),
          COALESCE(attributed.sku_name, ''),
          COALESCE(attributed.region, ''),
          COALESCE(attributed.org, ''),
          COALESCE(attributed.repo, ''),
          COALESCE(attributed.target_branch, ''),
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
        summary.service_name,
        summary.sku_name,
        summary.region,
        summary.org,
        summary.repo,
        summary.target_branch,
        summary.author,
        {_MATCHED_OWNER} AS owner,
        CASE
          WHEN COALESCE(
            override_employee.id,
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN CONCAT(
            'employee:',
            CAST(COALESCE(
              override_employee.id,
              github_employee.id,
              email_employee.id,
              normalized_employee.id
            ) AS CHAR)
          )
          WHEN summary.author IS NOT NULL THEN CONCAT('author:', LOWER(summary.author))
          ELSE 'unattributed'
        END AS attribution_key,
        CASE
          WHEN override_employee.id IS NOT NULL THEN 'author_override'
          WHEN github_employee.id IS NOT NULL THEN 'author_github'
          WHEN email_employee.id IS NOT NULL THEN 'author_email'
          WHEN normalized_employee.id IS NOT NULL THEN 'author_normalized'
          WHEN summary.author IS NOT NULL THEN 'author_label'
          ELSE 'missing_author'
        END AS attribution_source,
        CASE
          WHEN COALESCE(
            override_employee.id,
            github_employee.id,
            email_employee.id,
            normalized_employee.id
          ) IS NOT NULL THEN 'matched'
          WHEN summary.author IS NOT NULL THEN 'unmatched'
          ELSE 'unattributed'
        END AS attribution_status,
        COALESCE(
          override_employee.id,
          github_employee.id,
          email_employee.id,
          normalized_employee.id
        ) AS employee_id,
        COALESCE(
          override_employee.group_id,
          github_employee.group_id,
          email_employee.group_id,
          normalized_employee.group_id
        ) AS group_id,
        COALESCE(
          override_employee.manager_id,
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
      LEFT JOIN roster_employees override_employee
        ON summary.author IS NOT NULL
       AND override_employee.email IS NOT NULL
       AND LOWER(override_employee.email) = LOWER({_SUMMARY_AUTHOR_OVERRIDE_EMAIL})
      LEFT JOIN roster_employees github_employee
        ON override_employee.id IS NULL
       AND summary.author IS NOT NULL
       AND github_employee.github_id IS NOT NULL
       AND LOWER(github_employee.github_id) = LOWER(summary.author)
      LEFT JOIN roster_employees email_employee
        ON github_employee.id IS NULL
       AND summary.author IS NOT NULL
       AND email_employee.email IS NOT NULL
       AND (
         LOWER(email_employee.email) = LOWER(summary.author)
         OR LOWER(SUBSTRING_INDEX(email_employee.email, '@', 1)) = LOWER(summary.author)
       )
      LEFT JOIN roster_employees normalized_employee
        ON github_employee.id IS NULL
       AND email_employee.id IS NULL
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
         override_employee.group_id,
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
      attributed.service_name,
      attributed.sku_name,
      attributed.region,
      attributed.org,
      attributed.repo,
      attributed.target_branch,
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


def _build_insert_attribution_daily_from_summary_with_tcms(tcms_table: str):
    return text(
        f"""
        INSERT INTO cost_attribution_daily (
          usage_date,
          vendor,
          account_id,
          service_name,
          sku_name,
          region,
          org,
          repo,
          target_branch,
          resource_name,
          vendor_tags_json,
          author,
          owner,
          service,
          project,
          service_exec_id,
          attribution_key,
          attribution_source,
          attribution_status,
          allocate_method,
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
          attributed.region,
          attributed.org,
          attributed.repo,
          attributed.target_branch,
          NULL AS resource_name,
          attributed.vendor_tags_json,
          attributed.author,
          attributed.owner,
          attributed.service,
          attributed.project,
          attributed.service_exec_id,
          attributed.attribution_key,
          attributed.attribution_source,
          attributed.attribution_status,
          attributed.allocate_method,
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
              COALESCE(attributed.service_name, ''),
              COALESCE(attributed.sku_name, ''),
              COALESCE(attributed.region, ''),
              COALESCE(attributed.org, ''),
              COALESCE(attributed.repo, ''),
              COALESCE(attributed.target_branch, ''),
              '',
              COALESCE(attributed.vendor_tags_json, ''),
              COALESCE(attributed.author, ''),
              COALESCE(attributed.owner, ''),
              COALESCE(attributed.service, ''),
              COALESCE(attributed.project, ''),
              COALESCE(attributed.service_exec_id, ''),
              COALESCE(attributed.attribution_key, ''),
              COALESCE(attributed.attribution_source, ''),
              COALESCE(attributed.attribution_status, ''),
              COALESCE(attributed.allocate_method, ''),
              COALESCE(CAST(attributed.employee_id AS CHAR), ''),
              COALESCE(CAST(attributed.group_id AS CHAR), ''),
              COALESCE(CAST(attributed.manager_id AS CHAR), '')
            ),
            256
          ) AS dimension_hash
        FROM (
          SELECT
            base.usage_date,
            base.vendor,
            base.account_id,
            base.service_name,
            base.sku_name,
            base.region,
            base.org,
            base.repo,
            base.target_branch,
            base.vendor_tags_json,
            base.author,
            {_TCMS_OWNER} AS owner,
            base.service,
            base.project,
            base.service_exec_id,
            CASE
              WHEN COALESCE(
                owner_email_employee.id,
                override_employee.id,
                github_employee.id,
                email_employee.id,
                normalized_employee.id
              ) IS NOT NULL THEN CONCAT(
                'employee:',
                CAST(COALESCE(
                  owner_email_employee.id,
                  override_employee.id,
                  github_employee.id,
                  email_employee.id,
                  normalized_employee.id
                ) AS CHAR)
              )
              WHEN base.identity_kind = 'owner_email' AND base.match_identity IS NOT NULL
                THEN CONCAT('owner_email:', LOWER(base.match_identity))
              WHEN base.identity_kind = 'author' AND base.match_identity IS NOT NULL
                THEN CONCAT('author:', LOWER(base.match_identity))
              ELSE 'unattributed'
            END AS attribution_key,
            CASE
              WHEN base.identity_kind = 'owner_email' THEN 'owner_email'
              WHEN override_employee.id IS NOT NULL THEN 'author_override'
              WHEN github_employee.id IS NOT NULL THEN 'author_github'
              WHEN email_employee.id IS NOT NULL THEN 'author_email'
              WHEN normalized_employee.id IS NOT NULL THEN 'author_normalized'
              WHEN base.identity_kind = 'author' THEN 'author_label'
              WHEN base.cluster IS NOT NULL THEN 'missing_label_allocation'
              ELSE 'missing_author'
            END AS attribution_source,
            CASE
              WHEN COALESCE(
                owner_email_employee.id,
                override_employee.id,
                github_employee.id,
                email_employee.id,
                normalized_employee.id
              ) IS NOT NULL THEN 'matched'
              WHEN base.match_identity IS NOT NULL THEN 'unmatched'
              ELSE 'unattributed'
            END AS attribution_status,
            base.allocate_method,
            COALESCE(
              owner_email_employee.id,
              override_employee.id,
              github_employee.id,
              email_employee.id,
              normalized_employee.id
            ) AS employee_id,
            COALESCE(
              owner_email_employee.group_id,
              override_employee.group_id,
              github_employee.group_id,
              email_employee.group_id,
              normalized_employee.group_id
            ) AS group_id,
            COALESCE(
              owner_email_employee.manager_id,
              override_employee.manager_id,
              github_employee.manager_id,
              email_employee.manager_id,
              normalized_employee.manager_id,
              matched_group.manager_id
            ) AS manager_id,
            base.list_cost,
            base.effective_cost,
            base.credit_amount,
            base.net_cost
          FROM (
            SELECT
              summary.usage_date,
              summary.vendor,
              summary.account_id,
              summary.service_name,
              summary.sku_name,
              summary.region,
              summary.org,
              summary.repo,
              summary.target_branch,
              CASE
                WHEN allocation.id IS NOT NULL OR summary.author IS NULL
                  THEN summary.vendor_tags_json
                ELSE NULL
              END AS vendor_tags_json,
              {_SUMMARY_CLUSTER} AS cluster,
              summary.author,
              CASE
                WHEN allocation.id IS NOT NULL THEN allocation.owner_email
                WHEN account_allocation.summary_id IS NULL AND summary.author IS NOT NULL
                  THEN summary.author
                ELSE NULL
              END AS owner,
              CASE
                WHEN allocation.id IS NOT NULL THEN allocation.owner_email
                WHEN account_allocation.summary_id IS NULL AND summary.author IS NOT NULL
                  THEN summary.author
                ELSE NULL
              END AS match_identity,
              CASE
                WHEN allocation.id IS NOT NULL THEN 'owner_email'
                WHEN account_allocation.summary_id IS NULL AND summary.author IS NOT NULL THEN 'author'
                ELSE NULL
              END AS identity_kind,
              CASE WHEN allocation.id IS NOT NULL THEN allocation.service ELSE NULL END AS service,
              CASE WHEN allocation.id IS NOT NULL THEN allocation.project ELSE NULL END AS project,
              CASE WHEN allocation.id IS NOT NULL THEN allocation.service_exec_id ELSE NULL END
                AS service_exec_id,
              CASE
                WHEN allocation.id IS NULL THEN NULL
                WHEN {_ALLOCATION_MATCH_CLUSTER} IS NOT NULL THEN 'logical'
                ELSE 'vendor_tag'
              END AS allocate_method,
              summary.list_cost,
              summary.effective_cost,
              summary.credit_amount,
              summary.net_cost
            FROM cost_bq_export_summary_daily summary
            LEFT JOIN (
              SELECT *
              FROM (
                SELECT
                  summary.id AS summary_id,
                  allocation.id,
                  allocation.owner_email,
                  allocation.service,
                  allocation.project,
                  allocation.service_exec_id,
                  allocation.match_tags_json,
                  ROW_NUMBER() OVER (
                    PARTITION BY summary.id
                    ORDER BY
                      JSON_LENGTH(allocation.match_tags_json) DESC,
                      CASE WHEN allocation.account_id = summary.account_id THEN 0 ELSE 1 END,
                      COALESCE(allocation.valid_from, '1900-01-01') DESC,
                      allocation.id DESC
                  ) AS match_rank
                FROM cost_bq_export_summary_daily summary
                JOIN (
                  SELECT
                    allocation_raw.*,
                    {_ALLOCATION_MATCH_TAGS_JSON} AS match_tags_json
                  FROM {tcms_table} allocation_raw
                ) allocation
                  ON summary.vendor_tags_json IS NOT NULL
                 AND allocation.vendor = summary.vendor
                 AND (allocation.account_id IS NULL OR allocation.account_id = summary.account_id)
                 AND allocation.match_tags_json IS NOT NULL
                 AND JSON_LENGTH(allocation.match_tags_json) > 0
                 AND JSON_CONTAINS(summary.vendor_tags_json, allocation.match_tags_json)
                 AND summary.usage_date >= COALESCE(allocation.valid_from, '1900-01-01')
                 AND summary.usage_date <= COALESCE(allocation.valid_to, '9999-12-31')
                WHERE summary.usage_date BETWEEN :start_date AND :end_date
                  AND summary.vendor = :vendor
                  AND summary.account_id = :account_id
              ) ranked_allocation
              WHERE match_rank = 1
            ) allocation
              ON allocation.summary_id = summary.id
            LEFT JOIN (
              SELECT DISTINCT summary.id AS summary_id
              FROM cost_bq_export_summary_daily summary
              JOIN {tcms_table} allocation_raw
                ON allocation_raw.vendor = summary.vendor
               AND allocation_raw.account_id = summary.account_id
               AND allocation_raw.owner_email IS NOT NULL
               AND summary.usage_date >= COALESCE(allocation_raw.valid_from, '1900-01-01')
               AND summary.usage_date <= COALESCE(allocation_raw.valid_to, '9999-12-31')
              WHERE summary.usage_date BETWEEN :start_date AND :end_date
                AND summary.vendor = :vendor
                AND summary.account_id = :account_id
            ) account_allocation
              ON account_allocation.summary_id = summary.id
            WHERE summary.usage_date BETWEEN :start_date AND :end_date
              AND summary.vendor = :vendor
              AND summary.account_id = :account_id
              AND NOT (
                summary.author IS NULL
                AND {_SUMMARY_CLUSTER} IS NULL
                AND {_SUMMARY_SHARED_POOL} IS NOT NULL
                AND allocation.id IS NULL
              )
          ) base
          LEFT JOIN roster_employees owner_email_employee
            ON base.identity_kind = 'owner_email'
           AND base.match_identity IS NOT NULL
           AND owner_email_employee.email IS NOT NULL
           AND LOWER(owner_email_employee.email) = LOWER(base.match_identity)
          LEFT JOIN roster_employees override_employee
            ON base.identity_kind = 'author'
           AND base.match_identity IS NOT NULL
           AND override_employee.email IS NOT NULL
           AND LOWER(override_employee.email) = LOWER({_BASE_IDENTITY_OVERRIDE_EMAIL})
          LEFT JOIN roster_employees github_employee
            ON override_employee.id IS NULL
           AND base.identity_kind = 'author'
           AND base.match_identity IS NOT NULL
           AND github_employee.github_id IS NOT NULL
           AND LOWER(github_employee.github_id) = LOWER(base.match_identity)
          LEFT JOIN roster_employees email_employee
            ON github_employee.id IS NULL
           AND base.identity_kind = 'author'
           AND base.match_identity IS NOT NULL
           AND email_employee.email IS NOT NULL
           AND (
             LOWER(email_employee.email) = LOWER(base.match_identity)
             OR LOWER(SUBSTRING_INDEX(email_employee.email, '@', 1)) = LOWER(base.match_identity)
           )
          LEFT JOIN roster_employees normalized_employee
            ON github_employee.id IS NULL
           AND email_employee.id IS NULL
           AND base.identity_kind = 'author'
           AND base.match_identity IS NOT NULL
           AND (
             normalized_employee.github_id IS NOT NULL
             OR normalized_employee.email IS NOT NULL
             OR normalized_employee.en_name IS NOT NULL
           )
           AND (
             {_NORMALIZED_BASE_IDENTITY} = {_NORMALIZED_GITHUB_ID}
             OR {_NORMALIZED_BASE_IDENTITY} = {_NORMALIZED_EMAIL_LOCAL}
             OR {_NORMALIZED_BASE_IDENTITY} = {_NORMALIZED_EN_NAME}
           )
          LEFT JOIN roster_groups matched_group
            ON matched_group.is_active = 1
           AND matched_group.id = COALESCE(
             owner_email_employee.group_id,
             override_employee.group_id,
             github_employee.group_id,
             email_employee.group_id,
             normalized_employee.group_id
           )
        ) attributed
        GROUP BY
          attributed.usage_date,
          attributed.vendor,
          attributed.account_id,
          attributed.service_name,
          attributed.sku_name,
          attributed.region,
          attributed.org,
          attributed.repo,
          attributed.target_branch,
          attributed.vendor_tags_json,
          attributed.author,
          attributed.owner,
          attributed.service,
          attributed.project,
          attributed.service_exec_id,
          attributed.attribution_key,
          attributed.attribution_source,
          attributed.attribution_status,
          attributed.allocate_method,
          attributed.employee_id,
          attributed.group_id,
          attributed.manager_id
        """
    )


def _build_insert_shared_attribution_daily_from_summary(tcms_table: str):
    return text(
        f"""
        INSERT INTO cost_attribution_daily (
          usage_date,
          vendor,
          account_id,
          service_name,
          sku_name,
          region,
          org,
          repo,
          target_branch,
          resource_name,
          vendor_tags_json,
          author,
          owner,
          service,
          project,
          service_exec_id,
          attribution_key,
          attribution_source,
          attribution_status,
          allocate_method,
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
        WITH allocation_match AS (
          SELECT *
          FROM (
            SELECT
              summary.id AS summary_id,
              allocation.id,
              allocation.service,
              allocation.project,
              allocation.match_tags_json,
              ROW_NUMBER() OVER (
                PARTITION BY summary.id
                ORDER BY
                  JSON_LENGTH(allocation.match_tags_json) DESC,
                  CASE WHEN allocation.account_id = summary.account_id THEN 0 ELSE 1 END,
                  COALESCE(allocation.valid_from, '1900-01-01') DESC,
                  allocation.id DESC
              ) AS match_rank
            FROM cost_bq_export_summary_daily summary
            JOIN (
              SELECT
                allocation_raw.*,
                {_ALLOCATION_MATCH_TAGS_JSON} AS match_tags_json
              FROM {tcms_table} allocation_raw
            ) allocation
              ON summary.vendor_tags_json IS NOT NULL
             AND allocation.vendor = summary.vendor
             AND (allocation.account_id IS NULL OR allocation.account_id = summary.account_id)
             AND allocation.match_tags_json IS NOT NULL
             AND JSON_LENGTH(allocation.match_tags_json) > 0
             AND JSON_CONTAINS(summary.vendor_tags_json, allocation.match_tags_json)
             AND summary.usage_date >= COALESCE(allocation.valid_from, '1900-01-01')
             AND summary.usage_date <= COALESCE(allocation.valid_to, '9999-12-31')
            WHERE summary.usage_date BETWEEN :start_date AND :end_date
              AND summary.vendor = :vendor
              AND summary.account_id = :account_id
          ) ranked_allocation
          WHERE match_rank = 1
        ),
        logical AS (
          SELECT
            summary.usage_date,
            summary.vendor,
            summary.account_id,
            {_SUMMARY_SHARED_POOL} AS shared_pool,
            allocation.service,
            allocation.project,
            SUM(summary.net_cost) AS project_logical_cost
          FROM cost_bq_export_summary_daily summary
          JOIN allocation_match allocation
            ON allocation.summary_id = summary.id
          WHERE summary.usage_date BETWEEN :start_date AND :end_date
            AND summary.vendor = :vendor
            AND summary.account_id = :account_id
            AND {_SUMMARY_CLUSTER} IS NOT NULL
          GROUP BY
            summary.usage_date,
            summary.vendor,
            summary.account_id,
            shared_pool,
            allocation.service,
            allocation.project
        ),
        pool_total AS (
          SELECT
            usage_date,
            vendor,
            account_id,
            shared_pool,
            SUM(project_logical_cost) AS pool_logical_cost,
            COUNT(*) AS allocation_count
          FROM logical
          GROUP BY usage_date, vendor, account_id, shared_pool
        ),
        shared_cost AS (
          SELECT
            summary.usage_date,
            summary.vendor,
            summary.account_id,
            summary.service_name,
            summary.sku_name,
            summary.region,
            summary.vendor_tags_json,
            {_SUMMARY_SHARED_POOL} AS shared_pool,
            SUM(summary.list_cost) AS list_cost,
            SUM(summary.effective_cost) AS effective_cost,
            SUM(summary.credit_amount) AS credit_amount,
            SUM(summary.net_cost) AS net_cost,
            COUNT(*) AS source_rows
          FROM cost_bq_export_summary_daily summary
          LEFT JOIN allocation_match allocation
            ON allocation.summary_id = summary.id
          WHERE summary.usage_date BETWEEN :start_date AND :end_date
            AND summary.vendor = :vendor
            AND summary.account_id = :account_id
            AND summary.author IS NULL
            AND {_SUMMARY_CLUSTER} IS NULL
            AND {_SUMMARY_SHARED_POOL} IS NOT NULL
            AND allocation.id IS NULL
          GROUP BY
            summary.usage_date,
            summary.vendor,
            summary.account_id,
            summary.service_name,
            summary.sku_name,
            summary.region,
            summary.vendor_tags_json,
            shared_pool
        )
        SELECT
          allocated.usage_date,
          allocated.vendor,
          allocated.account_id,
          allocated.service_name,
          allocated.sku_name,
          allocated.region,
          NULL AS org,
          NULL AS repo,
          NULL AS target_branch,
          NULL AS resource_name,
          allocated.vendor_tags_json,
          NULL AS author,
          NULL AS owner,
          allocated.service,
          allocated.project,
          NULL AS service_exec_id,
          allocated.attribution_key,
          allocated.attribution_source,
          allocated.attribution_status,
          allocated.allocate_method,
          NULL AS employee_id,
          NULL AS group_id,
          NULL AS manager_id,
          NULL AS usage_seconds,
          allocated.list_cost,
          allocated.effective_cost,
          allocated.credit_amount,
          allocated.net_cost,
          allocated.source_rows,
          SHA2(
            CONCAT_WS(
              '|',
              DATE_FORMAT(allocated.usage_date, '%Y-%m-%d'),
              COALESCE(allocated.vendor, ''),
              COALESCE(allocated.account_id, ''),
              COALESCE(allocated.service_name, ''),
              COALESCE(allocated.sku_name, ''),
              COALESCE(allocated.region, ''),
              '',
              '',
              '',
              '',
              COALESCE(allocated.vendor_tags_json, ''),
              '',
              '',
              COALESCE(allocated.service, ''),
              COALESCE(allocated.project, ''),
              '',
              COALESCE(allocated.attribution_key, ''),
              COALESCE(allocated.attribution_source, ''),
              COALESCE(allocated.attribution_status, ''),
              COALESCE(allocated.allocate_method, ''),
              '',
              '',
              ''
            ),
            256
          ) AS dimension_hash
        FROM (
          SELECT
            shared_cost.usage_date,
            shared_cost.vendor,
            shared_cost.account_id,
            shared_cost.service_name,
            shared_cost.sku_name,
            shared_cost.region,
            shared_cost.vendor_tags_json,
            shared_cost.shared_pool,
            logical.service,
            logical.project,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN 'unattributed'
              ELSE CONCAT(
                'shared:',
                COALESCE(logical.service, ''),
                ':',
                COALESCE(logical.project, ''),
                ':',
                COALESCE(shared_cost.shared_pool, '')
              )
            END AS attribution_key,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN 'missing_label_allocation'
              ELSE 'label_shared'
            END AS attribution_source,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN 'unattributed'
              ELSE 'shared'
            END AS attribution_status,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN NULL
              ELSE 'shared_weighted'
            END AS allocate_method,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN shared_cost.list_cost
              WHEN pool_total.pool_logical_cost > 0
                THEN ROUND(
                  shared_cost.list_cost
                  * logical.project_logical_cost
                  / pool_total.pool_logical_cost,
                  2
                )
              ELSE ROUND(shared_cost.list_cost / pool_total.allocation_count, 2)
            END AS list_cost,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN shared_cost.effective_cost
              WHEN pool_total.pool_logical_cost > 0
                THEN ROUND(
                  shared_cost.effective_cost
                  * logical.project_logical_cost
                  / pool_total.pool_logical_cost,
                  2
                )
              ELSE ROUND(shared_cost.effective_cost / pool_total.allocation_count, 2)
            END AS effective_cost,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN shared_cost.credit_amount
              WHEN pool_total.pool_logical_cost > 0
                THEN ROUND(
                  shared_cost.credit_amount
                  * logical.project_logical_cost
                  / pool_total.pool_logical_cost,
                  2
                )
              ELSE ROUND(shared_cost.credit_amount / pool_total.allocation_count, 2)
            END AS credit_amount,
            CASE
              WHEN pool_total.allocation_count IS NULL THEN shared_cost.net_cost
              WHEN pool_total.pool_logical_cost > 0
                THEN ROUND(
                  shared_cost.net_cost
                  * logical.project_logical_cost
                  / pool_total.pool_logical_cost,
                  2
                )
              ELSE ROUND(shared_cost.net_cost / pool_total.allocation_count, 2)
            END AS net_cost,
            shared_cost.source_rows
          FROM shared_cost
          LEFT JOIN logical
            ON logical.usage_date = shared_cost.usage_date
           AND logical.vendor = shared_cost.vendor
           AND logical.account_id = shared_cost.account_id
           AND logical.shared_pool = shared_cost.shared_pool
          LEFT JOIN pool_total
            ON pool_total.usage_date = shared_cost.usage_date
           AND pool_total.vendor = shared_cost.vendor
           AND pool_total.account_id = shared_cost.account_id
           AND pool_total.shared_pool = shared_cost.shared_pool
        ) allocated
        """
    )
