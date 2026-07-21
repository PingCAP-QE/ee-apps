from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any


DEFAULT_COST_OWNER_AUTHOR = "wei_zheng"
_GCP_MULTI_REGION_LOCATIONS = ("us", "eu", "asia", "nam4", "eur4")
_GCP_MULTI_REGION_SKU_PATTERN = r"\b(multi[- ]region|dual[- ]region)\b"
_GCP_CROSS_REGION_SKU_PATTERN = r"\b(inter[- ]region|cross[- ]region|replication)\b"
_GCP_TRANSFER_SKU_PATTERN = r"\b(data transfer|egress)\b.*\b(to|from|within)\b"


def _region_expr() -> str:
    lower_location = "LOWER(COALESCE(location.location, ''))"
    lower_sku = "LOWER(COALESCE(sku.description, ''))"
    multi_region_locations = ", ".join(repr(location) for location in _GCP_MULTI_REGION_LOCATIONS)
    return f"""
CASE
  WHEN NULLIF(location.region, '') IS NOT NULL THEN location.region
  WHEN {lower_location} IN ({multi_region_locations})
    THEN 'multi-region'
  WHEN REGEXP_CONTAINS({lower_sku}, r'{_GCP_MULTI_REGION_SKU_PATTERN}')
    THEN 'multi-region'
  WHEN REGEXP_CONTAINS(
    {lower_sku},
    r'{_GCP_CROSS_REGION_SKU_PATTERN}'
  )
    THEN 'cross-region'
  WHEN REGEXP_CONTAINS(
    {lower_sku},
    r'{_GCP_TRANSFER_SKU_PATTERN}'
  )
    THEN 'cross-region'
  WHEN {lower_location} = 'global'
    THEN 'global'
  ELSE 'unknown'
END
""".strip()


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


def fetch_gcp_billing_summary_rows(
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
    query = build_gcp_billing_summary_query(billing_table=billing_table, limit=limit)
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


def fetch_gcp_unmatched_resource_rows(
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
    query = build_gcp_unmatched_resource_query(billing_table=billing_table, limit=limit)
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


def build_gcp_billing_query(*, billing_table: str, limit: int | None = None) -> str:
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    author_expr = _author_expr_with_overrides()
    target_branch_expr = _target_branch_expr()
    region_expr = _region_expr()
    return f"""
WITH normalized AS (
  SELECT
    billing_account_id,
    project.id AS account_id,
    DATE(usage_start_time) AS usage_date,
    service.description AS service_name,
    sku.description AS sku_name,
    {region_expr} AS region,
    {_label_expr(("k8s-namespace", "namespace"))} AS namespace,
    {author_expr} AS author,
    {_label_expr(("k8s-label/org", "org"))} AS org,
    {_label_expr(("k8s-label/repo", "repo"))} AS repo,
    {target_branch_expr} AS target_branch,
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
  target_branch,
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
  target_branch,
  resource_name
ORDER BY usage_date, service_name, sku_name, resource_name{limit_clause}
""".strip()


def build_gcp_billing_summary_query(*, billing_table: str, limit: int | None = None) -> str:
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    author_expr = _author_expr_with_overrides()
    target_branch_expr = _target_branch_expr()
    region_expr = _region_expr()
    return f"""
SELECT
  'gcp' AS vendor,
  project.id AS account_id,
  billing_account_id,
  _PARTITIONDATE AS export_partition_date,
  DATE(usage_start_time) AS usage_date,
  service.description AS service_name,
  sku.description AS sku_name,
  NULL AS usage_type,
  {region_expr} AS region,
  {author_expr} AS author,
  {_label_expr(("k8s-label/org", "org"))} AS org,
  {_label_expr(("k8s-label/repo", "repo"))} AS repo,
  {target_branch_expr} AS target_branch,
  ROUND(SUM(cost_at_list), 2) AS list_cost,
  ROUND(SUM(cost), 2) AS effective_cost,
  ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 2)
    AS credit_amount,
  ROUND(SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 2)
    AS net_cost,
  MAX(export_time) AS source_export_time
FROM `{billing_table}`
WHERE _PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end
  AND project.id = @account_id
  AND DATE(usage_start_time) >= @earliest_usage_date
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
  target_branch
ORDER BY export_partition_date, usage_date, service_name, sku_name, region, author, org, repo, target_branch{limit_clause}
""".strip()


def build_gcp_unmatched_resource_query(*, billing_table: str, limit: int | None = None) -> str:
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    namespace_expr = _label_expr(("k8s-namespace", "namespace"))
    author_expr = _author_expr_with_overrides()
    org_expr = _label_expr(("k8s-label/org", "org"))
    repo_expr = _label_expr(("k8s-label/repo", "repo"))
    target_branch_expr = _target_branch_expr()
    workload_expr = _label_expr(("k8s-workload-name",))
    return f"""
WITH normalized AS (
  SELECT
    billing_account_id,
    project.id AS account_id,
    _PARTITIONDATE AS export_partition_date,
    DATE(usage_start_time) AS usage_date,
    service.description AS service_name,
    sku.description AS sku_name,
    {namespace_expr} AS namespace,
    {author_expr} AS author,
    {org_expr} AS org,
    {repo_expr} AS repo,
    {target_branch_expr} AS target_branch,
    COALESCE(
      {workload_expr},
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
  WHERE _PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end
    AND project.id = @account_id
    AND DATE(usage_start_time) BETWEEN @usage_start_date AND @usage_end_date
)
SELECT
  'gcp' AS vendor,
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
  target_branch,
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


def _author_expr_with_overrides() -> str:
    label_author = _label_expr(("k8s-label/author", "author"))
    return f"""
    COALESCE(
      {label_author},
      CASE
        WHEN service.description = 'Cloud Logging' THEN '{DEFAULT_COST_OWNER_AUTHOR}'
        WHEN sku.description = 'Compute Flexible Committed Use Discounts - 3 Year'
          THEN '{DEFAULT_COST_OWNER_AUTHOR}'
        WHEN sku.description = 'Compute Flexible Committed Use Discounts - 1 Year'
          THEN '{DEFAULT_COST_OWNER_AUTHOR}'
        ELSE NULL
      END
    )
    """.strip()


def _target_branch_expr() -> str:
    return _label_expr(
        (
            "k8s-label/prow.k8s.io/refs.base_ref",
            "prow.k8s.io/refs.base_ref",
        )
    )


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
