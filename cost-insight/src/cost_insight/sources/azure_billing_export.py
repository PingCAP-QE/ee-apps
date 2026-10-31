from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from typing import Any

from cost_insight.common.bigquery import build_billing_bigquery_client

DEFAULT_AZURE_BILLING_TABLE = "gcp-digital-bi.azure_billing.azure_billing_cost_*"


_AZURE_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+_\*$")


def _validate_billing_table(billing_table: str) -> str:
    if not _AZURE_TABLE_RE.fullmatch(billing_table):
        raise ValueError("Azure billing table must be a project.dataset.table_* identifier")
    return billing_table


def fetch_azure_billing_summary_rows(
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

    billing_table = _validate_billing_table(billing_table)
    client = build_billing_bigquery_client()
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
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
    )
    for row in client.query(
        build_azure_billing_summary_query(billing_table=billing_table, limit=limit),
        job_config=job_config,
    ).result(page_size=page_size):
        yield dict(row.items())


def azure_warehouse_tables(billing_table: str, start: date, end: date) -> dict[date, str]:
    billing_table = _validate_billing_table(billing_table)
    if not 0 <= (end - start).days <= 13:
        raise ValueError("Azure warehouse reads require a window of at most 14 days")
    months = sorted({start.replace(day=1), end.replace(day=1)})
    return {month: billing_table[:-1] + month.strftime("%Y%m%d") for month in months}


def build_azure_warehouse_relation(billing_table: str, start: date, end: date) -> str:
    # Never let BigQuery resolve a mixed-schema MCA/EA wildcard relation.
    return "(\n" + "\nUNION ALL\n".join(
        f"SELECT raw.*, '{month:%Y%m%d}' AS _TABLE_SUFFIX FROM `{table_id}` AS raw "
        "WHERE CAST(date AS DATE) BETWEEN @earliest_usage_date AND @usage_end_date"
        for month, table_id in azure_warehouse_tables(billing_table, start, end).items()
    ) + "\n)"


def build_azure_billing_summary_query(
    *, billing_table: str, limit: int | None = None,
    warehouse_window: tuple[date, date] | None = None,
) -> str:
    billing_table = _validate_billing_table(billing_table)
    relation = (f"`{billing_table}`" if warehouse_window is None else
                build_azure_warehouse_relation(billing_table, *warehouse_window))
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    return f"""
WITH normalized AS (
  SELECT 'azure' AS vendor, CAST(SubscriptionId AS STRING) AS account_id,
    NULLIF(CAST(billingAccountId AS STRING), '') AS billing_account_id,
    SAFE.PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) AS export_partition_date,
    CAST(date AS DATE) AS usage_date,
    NULLIF(CAST(consumedService AS STRING), '') AS service_name,
    COALESCE(NULLIF(CAST(meterName AS STRING), ''), NULLIF(CAST(ProductName AS STRING), '')) AS sku_name,
    NULLIF(CAST(chargeType AS STRING), '') AS usage_type,
    NULLIF(CAST(billingCurrency AS STRING), '') AS currency,
    COALESCE(NULLIF(CAST(location AS STRING), ''), NULLIF(CAST(resourceLocation AS STRING), ''), NULLIF(CAST(meterRegion AS STRING), '')) AS region,
    NULLIF(CAST(ResourceId AS STRING), '') AS resource_name,
    NULLIF(CAST(tags AS STRING), '') AS vendor_tags_json,
    SAFE_CAST(paygCostInBillingCurrency AS NUMERIC) AS list_cost,
    SAFE_CAST(costInBillingCurrency AS NUMERIC) AS effective_cost,
    CAST(0 AS NUMERIC) AS credit_amount,
    SAFE_CAST(costInBillingCurrency AS NUMERIC) AS net_cost
  FROM {relation}
  WHERE REGEXP_CONTAINS(_TABLE_SUFFIX, r'^[0-9]{{8}}$')
    AND SAFE.PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) IS NOT NULL
    AND DATE_TRUNC(SAFE.PARSE_DATE('%Y%m%d', _TABLE_SUFFIX), MONTH)
        BETWEEN DATE_TRUNC(@export_partition_start, MONTH)
        AND DATE_TRUNC(@export_partition_end, MONTH)
    AND CAST(SubscriptionId AS STRING) = @account_id
    AND CAST(date AS DATE) >= @earliest_usage_date
)
SELECT vendor, account_id, billing_account_id, export_partition_date, usage_date, service_name, sku_name, usage_type, currency, region, resource_name, vendor_tags_json,
  ROUND(SUM(list_cost), 9) AS list_cost, ROUND(SUM(effective_cost), 9) AS effective_cost,
  ROUND(SUM(credit_amount), 9) AS credit_amount, ROUND(SUM(net_cost), 9) AS net_cost,
  CAST(NULL AS TIMESTAMP) AS source_export_time
FROM normalized
GROUP BY vendor, account_id, billing_account_id, export_partition_date, usage_date, service_name, sku_name, usage_type, currency, region, resource_name, vendor_tags_json
ORDER BY export_partition_date, usage_date, service_name, sku_name, region, resource_name{limit_clause}
""".strip()
