from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any


def fetch_gcp_billing_rows(
    *,
    billing_table: str,
    account_id: str,
    start_date: date,
    end_date: date,
    page_size: int,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    from google.cloud import bigquery

    client = bigquery.Client()
    query = build_gcp_billing_query(billing_table=billing_table, limit=limit)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date.isoformat()),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date.isoformat()),
        ]
    )
    rows = client.query(query, job_config=job_config).result(page_size=page_size)
    for row in rows:
        yield dict(row.items())


def build_gcp_billing_query(*, billing_table: str, limit: int | None = None) -> str:
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    return f"""
WITH normalized AS (
  SELECT
    billing_account_id,
    project.id AS account_id,
    DATE(usage_start_time) AS usage_date,
    service.description AS service_name,
    sku.description AS sku_name,
    location.region AS region,
    {_label_expr(("k8s-namespace", "namespace"))} AS namespace,
    {_label_expr(("k8s-label/author", "author"))} AS author,
    {_label_expr(("k8s-label/org", "org"))} AS org,
    {_label_expr(("k8s-label/repo", "repo"))} AS repo,
    COALESCE(
      {_label_expr(("k8s-workload-name",))},
      NULLIF(resource.name, ''),
      NULLIF(resource.global_name, '')
    ) AS resource_name,
    LOWER(usage.pricing_unit) AS pricing_unit,
    usage.amount_in_pricing_units AS amount_in_pricing_units,
    cost_at_list,
    cost,
    IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0) AS credit_amount,
    export_time
  FROM `{billing_table}`
  WHERE project.id = @account_id
    AND DATE(usage_start_time) BETWEEN @start_date AND @end_date
)
SELECT
  'gcp' AS vendor,
  account_id,
  billing_account_id,
  usage_date,
  service_name,
  sku_name,
  region,
  namespace,
  author,
  org,
  repo,
  resource_name,
  CASE
    WHEN COUNTIF(pricing_unit IS NULL OR pricing_unit NOT IN ('hour', 'minute', 'second')) > 0
      THEN NULL
    WHEN COUNTIF(pricing_unit = 'hour') = COUNT(*)
      THEN ROUND(SUM(amount_in_pricing_units) * 3600, 2)
    WHEN COUNTIF(pricing_unit = 'minute') = COUNT(*)
      THEN ROUND(SUM(amount_in_pricing_units) * 60, 2)
    WHEN COUNTIF(pricing_unit = 'second') = COUNT(*)
      THEN ROUND(SUM(amount_in_pricing_units), 2)
    ELSE NULL
  END AS usage_seconds,
  ROUND(SUM(cost_at_list), 2) AS list_cost,
  ROUND(SUM(cost), 2) AS effective_cost,
  ROUND(SUM(credit_amount), 2) AS credit_amount,
  ROUND(SUM(cost + credit_amount), 2) AS net_cost,
  MAX(export_time) AS source_export_time
FROM normalized
GROUP BY
  account_id,
  billing_account_id,
  usage_date,
  service_name,
  sku_name,
  region,
  namespace,
  author,
  org,
  repo,
  resource_name
ORDER BY usage_date, service_name, sku_name, resource_name{limit_clause}
""".strip()


def _label_expr(keys: Iterable[str]) -> str:
    key_list = ", ".join(repr(key) for key in keys)
    return f"""
    ARRAY(
      SELECT label.value
      FROM UNNEST(labels) AS label
      WHERE label.key IN ({key_list})
      LIMIT 1
    )[SAFE_OFFSET(0)]
    """.strip()


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
