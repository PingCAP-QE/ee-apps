from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from typing import Any

_BIGQUERY_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_AWS_CE_UNBLENDED_LINE_ITEM_TYPES = "'Usage', 'SavingsPlanCoveredUsage'"
_AWS_TIDB_CLOUD_F04_USEDBY = "prod-us-west-2-f04"


def fetch_aws_flat_cur_summary_rows(
    *,
    billing_table: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    earliest_usage_date: date,
    usage_end_date: date | None = None,
    page_size: int,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    from google.cloud import bigquery

    client = bigquery.Client()
    query = build_aws_flat_cur_summary_query(
        billing_table=billing_table,
        export_partition_start=export_partition_start,
        export_partition_end=export_partition_end,
        usage_end_date=usage_end_date,
        limit=limit,
    )
    query_parameters = [
        bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
        bigquery.ScalarQueryParameter("usedby", "STRING", _AWS_TIDB_CLOUD_F04_USEDBY),
        bigquery.ScalarQueryParameter(
            "export_partition_start", "DATE", export_partition_start.isoformat()
        ),
        bigquery.ScalarQueryParameter(
            "export_partition_end", "DATE", export_partition_end.isoformat()
        ),
        bigquery.ScalarQueryParameter(
            "earliest_usage_date", "DATE", earliest_usage_date.isoformat()
        ),
    ]
    if usage_end_date is not None:
        query_parameters.append(
            bigquery.ScalarQueryParameter("usage_end_date", "DATE", usage_end_date.isoformat())
        )
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    rows = client.query(query, job_config=job_config).result(page_size=page_size)
    for row in rows:
        yield dict(row.items())


def build_aws_flat_cur_summary_query(
    *,
    billing_table: str,
    export_partition_start: date,
    export_partition_end: date,
    usage_end_date: date | None = None,
    limit: int | None = None,
) -> str:
    usage_end_clause = (
        "\n    AND DATE(line_item_usage_start_date) <= @usage_end_date"
        if usage_end_date is not None
        else ""
    )
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    return f"""
WITH normalized AS (
  SELECT
    line_item_usage_account_id AS account_id,
    NULLIF(bill_payer_account_id, '') AS billing_account_id,
    DATE(bill_billing_period_start_date) AS export_partition_date,
    DATE(line_item_usage_start_date) AS usage_date,
    COALESCE(NULLIF(product_servicecode, ''), NULLIF(line_item_product_code, '')) AS service_name,
    COALESCE(
      NULLIF(product_sku, ''),
      NULLIF(line_item_usage_type, ''),
      NULLIF(line_item_line_item_description, '')
    ) AS sku_name,
    NULLIF(line_item_usage_type, '') AS usage_type,
    COALESCE(
      NULLIF(product_region_code, ''),
      REGEXP_EXTRACT(NULLIF(line_item_availability_zone, ''), r'^([a-z]{{2}}(?:-gov)?-[a-z]+-[0-9]+)'),
      NULLIF(product_to_region_code, ''),
      NULLIF(product_from_region_code, ''),
      NULLIF(savings_plan_region, '')
    ) AS region,
    NULLIF(TRIM(resource_tags_user_usedby), '') AS author,
    NULLIF(TRIM(resource_tags_user_tenant), '') AS org,
    NULLIF(TRIM(resource_tags_user_project), '') AS repo,
    NULLIF(TRIM(resource_tags_user_cluster), '') AS `cluster`,
    CAST(COALESCE(line_item_unblended_cost, 0) AS BIGNUMERIC) AS list_cost,
    CAST(COALESCE(line_item_unblended_cost, line_item_blended_cost, 0) AS BIGNUMERIC)
      AS effective_cost,
    line_item_usage_end_date AS source_export_time
  FROM {_quote_bigquery_table(billing_table)}
  WHERE line_item_usage_account_id = @account_id
    AND DATE(bill_billing_period_start_date) BETWEEN @export_partition_start AND @export_partition_end
    AND DATE(line_item_usage_start_date) >= @earliest_usage_date{usage_end_clause}
    AND line_item_currency_code = 'USD'
    AND resource_tags_user_usedby = @usedby
    AND line_item_line_item_type IN ({_AWS_CE_UNBLENDED_LINE_ITEM_TYPES})
)
SELECT
  'aws' AS vendor,
  account_id,
  billing_account_id,
  export_partition_date,
  usage_date,
  service_name,
  sku_name,
  MIN(usage_type) AS usage_type,
  region,
  author,
  org,
  repo,
  CASE
    WHEN `cluster` IS NULL THEN NULL
    ELSE TO_JSON_STRING(STRUCT(`cluster` AS cluster))
  END AS vendor_tags_json,
  SUM(list_cost) AS list_cost,
  ROUND(SUM(effective_cost), 2) AS effective_cost,
  CAST(0 AS BIGNUMERIC) AS credit_amount,
  ROUND(SUM(effective_cost), 2) AS net_cost,
  MAX(source_export_time) AS source_export_time
FROM normalized
GROUP BY
  account_id,
  billing_account_id,
  export_partition_date,
  usage_date,
  service_name,
  sku_name,
  region,
  author,
  org,
  repo,
  vendor_tags_json
ORDER BY
  export_partition_date,
  usage_date,
  service_name,
  sku_name,
  region,
  author,
  org,
  repo,
  vendor_tags_json{limit_clause}
""".strip()


def _quote_bigquery_table(table: str) -> str:
    if not _BIGQUERY_TABLE_RE.fullmatch(table):
        raise ValueError(f"Invalid BigQuery table identifier: {table!r}")
    return f"`{table}`"
