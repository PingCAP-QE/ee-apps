from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any


def fetch_aws_billing_summary_rows(
    *,
    billing_table: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    earliest_usage_date: date,
    page_size: int,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    from google.cloud import bigquery

    client = bigquery.Client()
    query = build_aws_billing_summary_query(billing_table=billing_table, limit=limit)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
            bigquery.ScalarQueryParameter(
                "export_partition_start",
                "DATE",
                export_partition_start.isoformat(),
            ),
            bigquery.ScalarQueryParameter(
                "export_partition_end",
                "DATE",
                export_partition_end.isoformat(),
            ),
            bigquery.ScalarQueryParameter(
                "earliest_usage_date",
                "DATE",
                earliest_usage_date.isoformat(),
            ),
        ]
    )
    rows = client.query(query, job_config=job_config).result(page_size=page_size)
    for row in rows:
        yield dict(row.items())


def fetch_aws_unmatched_resource_rows(
    *,
    billing_table: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    usage_start_date: date,
    usage_end_date: date,
    page_size: int,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    from google.cloud import bigquery

    client = bigquery.Client()
    query = build_aws_unmatched_resource_query(billing_table=billing_table, limit=limit)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
            bigquery.ScalarQueryParameter(
                "export_partition_start",
                "DATE",
                export_partition_start.isoformat(),
            ),
            bigquery.ScalarQueryParameter(
                "export_partition_end",
                "DATE",
                export_partition_end.isoformat(),
            ),
            bigquery.ScalarQueryParameter("usage_start_date", "DATE", usage_start_date.isoformat()),
            bigquery.ScalarQueryParameter("usage_end_date", "DATE", usage_end_date.isoformat()),
        ]
    )
    rows = client.query(query, job_config=job_config).result(page_size=page_size)
    for row in rows:
        yield dict(row.items())


def build_aws_billing_summary_query(*, billing_table: str, limit: int | None = None) -> str:
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    return f"""
WITH normalized AS (
  SELECT
    line_item_usage_account_id AS account_id,
    NULLIF(bill_payer_account_id, '') AS billing_account_id,
    PARSE_DATE('%Y%m%d', billing_month) AS export_partition_date,
    DATE(line_item_usage_start_date) AS usage_date,
    COALESCE(NULLIF(product_servicecode, ''), NULLIF(line_item_product_code, '')) AS service_name,
    COALESCE(
      NULLIF(product_sku, ''),
      NULLIF(line_item_usage_type, ''),
      NULLIF(line_item_line_item_description, '')
    ) AS sku_name,
    COALESCE(
      NULLIF(product_region_code, ''),
      REGEXP_EXTRACT(NULLIF(line_item_availability_zone, ''), r'^([a-z]{2}(?:-gov)?-[a-z]+-[0-9]+)'),
      NULLIF(product_to_region_code, ''),
      NULLIF(product_from_region_code, ''),
      NULLIF(savings_plan_region, '')
    ) AS region,
    NULLIF(tag_used_by, '') AS author,
    NULLIF(tag_tenant, '') AS org,
    NULLIF(tag_project, '') AS repo,
    (
      SELECT NULLIF(kv.value, '')
      FROM UNNEST(resource_tags.key_value) AS kv
      WHERE kv.key = 'user_shared_pool'
      LIMIT 1
    ) AS shared_pool,
    NULLIF(tag_cluster, '') AS `cluster`,
    pricing_unit,
    line_item_usage_amount,
    COALESCE(pricing_public_on_demand_cost, 0) AS list_cost,
    COALESCE(line_item_unblended_cost, line_item_blended_cost, 0) AS effective_cost,
    COALESCE(
      line_item_net_unblended_cost,
      line_item_unblended_cost,
      line_item_blended_cost,
      0
    ) AS net_cost,
    line_item_usage_end_date AS source_export_time
  FROM `{billing_table}`
  WHERE line_item_usage_account_id = @account_id
    AND PARSE_DATE('%Y%m%d', billing_month) BETWEEN @export_partition_start AND @export_partition_end
    AND DATE(line_item_usage_start_date) >= @earliest_usage_date
)
SELECT
  'aws' AS vendor,
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
  CASE
    WHEN shared_pool IS NULL AND `cluster` IS NULL THEN NULL
    ELSE TO_JSON_STRING(STRUCT(`cluster` AS cluster, shared_pool AS shared_pool))
  END AS vendor_tags_json,
  ROUND(SUM(list_cost), 2) AS list_cost,
  ROUND(SUM(effective_cost), 2) AS effective_cost,
  ROUND(SUM(net_cost - effective_cost), 2) AS credit_amount,
  ROUND(SUM(net_cost), 2) AS net_cost,
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


def build_aws_unmatched_resource_query(*, billing_table: str, limit: int | None = None) -> str:
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    return f"""
WITH normalized AS (
  SELECT
    line_item_usage_account_id AS account_id,
    NULLIF(bill_payer_account_id, '') AS billing_account_id,
    PARSE_DATE('%Y%m%d', billing_month) AS export_partition_date,
    DATE(line_item_usage_start_date) AS usage_date,
    COALESCE(NULLIF(product_servicecode, ''), NULLIF(line_item_product_code, '')) AS service_name,
    COALESCE(
      NULLIF(product_sku, ''),
      NULLIF(line_item_usage_type, ''),
      NULLIF(line_item_line_item_description, '')
    ) AS sku_name,
    NULL AS namespace,
    NULLIF(tag_used_by, '') AS author,
    NULLIF(tag_tenant, '') AS org,
    NULLIF(tag_project, '') AS repo,
    (
      SELECT NULLIF(kv.value, '')
      FROM UNNEST(resource_tags.key_value) AS kv
      WHERE kv.key = 'user_shared_pool'
      LIMIT 1
    ) AS shared_pool,
    NULLIF(tag_cluster, '') AS `cluster`,
    COALESCE(
      NULLIF(line_item_resource_id, ''),
      NULLIF(split_line_item_parent_resource_id, ''),
      NULLIF(line_item_line_item_description, '')
    ) AS resource_name,
    LOWER(pricing_unit) AS pricing_unit,
    line_item_usage_amount,
    COALESCE(pricing_public_on_demand_cost, 0) AS list_cost,
    COALESCE(line_item_unblended_cost, line_item_blended_cost, 0) AS effective_cost,
    COALESCE(
      line_item_net_unblended_cost,
      line_item_unblended_cost,
      line_item_blended_cost,
      0
    ) AS net_cost,
    line_item_usage_end_date AS source_export_time
  FROM `{billing_table}`
  WHERE line_item_usage_account_id = @account_id
    AND PARSE_DATE('%Y%m%d', billing_month) BETWEEN @export_partition_start AND @export_partition_end
    AND DATE(line_item_usage_start_date) BETWEEN @usage_start_date AND @usage_end_date
)
SELECT
  'aws' AS vendor,
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
  CASE
    WHEN shared_pool IS NULL AND `cluster` IS NULL THEN NULL
    ELSE TO_JSON_STRING(STRUCT(`cluster` AS cluster, shared_pool AS shared_pool))
  END AS vendor_tags_json,
  resource_name,
  CASE
    WHEN COUNTIF(pricing_unit IS NULL OR pricing_unit NOT IN ('hour', 'minute', 'second')) > 0
      THEN NULL
    WHEN COUNTIF(pricing_unit = 'hour') = COUNT(*)
      THEN ROUND(SUM(line_item_usage_amount) * 3600, 2)
    WHEN COUNTIF(pricing_unit = 'minute') = COUNT(*)
      THEN ROUND(SUM(line_item_usage_amount) * 60, 2)
    WHEN COUNTIF(pricing_unit = 'second') = COUNT(*)
      THEN ROUND(SUM(line_item_usage_amount), 2)
    ELSE NULL
  END AS usage_seconds,
  ROUND(SUM(list_cost), 2) AS list_cost,
  ROUND(SUM(effective_cost), 2) AS effective_cost,
  ROUND(SUM(net_cost - effective_cost), 2) AS credit_amount,
  ROUND(SUM(net_cost), 2) AS net_cost,
  MAX(source_export_time) AS source_export_time
FROM normalized
WHERE resource_name IS NOT NULL
  AND resource_name <> ''
GROUP BY
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
  vendor_tags_json,
  resource_name
ORDER BY usage_date, service_name, sku_name, resource_name{limit_clause}
""".strip()
