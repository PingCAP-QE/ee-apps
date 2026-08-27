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


def build_gcp_billing_summary_query(*, billing_table: str, limit: int | None = None) -> str:
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    author_expr = _author_expr_with_overrides()
    target_branch_expr = _target_branch_expr()
    region_expr = _region_expr()
    cluster_name = _label_expr(("goog-k8s-cluster-name",))
    cluster_location = _label_expr(("goog-k8s-cluster-location",))
    namespace = _label_expr(("k8s-namespace", "namespace"))
    workload_name = _label_expr(("k8s-workload-name",))
    workload_type = _label_expr(("k8s-workload-type",))
    is_gke = f"(service.description = 'Kubernetes Engine' OR {cluster_name} IS NOT NULL OR ({namespace} IS NOT NULL AND {workload_name} IS NOT NULL) OR {namespace} LIKE 'kube:%' OR {namespace} LIKE 'goog-k8s-%')"
    is_direct = f"({namespace} IS NOT NULL AND {workload_name} IS NOT NULL AND {namespace} NOT LIKE 'kube:%' AND {namespace} NOT LIKE 'goog-k8s-%')"
    residual_type = f"""
CASE
  WHEN {namespace} = 'kube:system-overhead' THEN 'system_overhead'
  WHEN {namespace} = 'kube:unallocated' THEN 'idle'
  WHEN {namespace} = 'goog-k8s-unknown' THEN 'unknown'
  WHEN {namespace} = 'goog-k8s-unsupported-sku' THEN 'unsupported'
  WHEN service.description = 'Kubernetes Engine' AND {workload_name} IS NULL THEN 'control_plane'
  WHEN {is_gke} THEN 'unclassified'
  ELSE NULL
END
""".strip()
    resource_name = f"""
CASE
  WHEN {is_direct} THEN {workload_name}
  WHEN {is_gke} THEN NULL
  ELSE COALESCE(NULLIF(resource.name, ''), NULLIF(resource.global_name, ''))
END
""".strip()
    cost_component = """
CASE
  WHEN service.description = 'Kubernetes Engine' THEN 'control_plane'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(core|cpu|vcpu)\\b') THEN 'cpu'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(ram|memory)\\b') THEN 'memory'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(gpu|nvidia)\\b') THEN 'gpu'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(disk|storage|hyperdisk|pd capacity)\\b') THEN 'storage'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(network|egress|data transfer)\\b') THEN 'network'
  ELSE 'other'
END
""".strip()
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
  {_org_expr()} AS org,
  {_repo_expr()} AS repo,
  {target_branch_expr} AS target_branch,
  {resource_name} AS resource_name,
  CASE WHEN {is_gke} THEN 'gke_cost_allocation_v1' ELSE NULL END AS source_schema_version,
  CASE WHEN {is_direct} THEN 'gke_direct' WHEN {is_gke} THEN 'gke_residual' ELSE 'direct' END AS source_allocation_scope,
  {cluster_name} AS cluster_name,
  {cluster_location} AS cluster_location,
  CASE WHEN {is_direct} THEN 'direct' WHEN {is_gke} THEN 'residual' ELSE NULL END AS kubernetes_cost_class,
  {residual_type} AS kubernetes_residual_type,
  CASE WHEN {is_gke} THEN {cost_component} ELSE NULL END AS kubernetes_cost_component,
  {namespace} AS namespace,
  {workload_name} AS workload_name,
  {workload_type} AS workload_type,
  ROUND(SUM(cost_at_list), 9) AS list_cost,
  ROUND(SUM(cost), 9) AS effective_cost,
  ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 9) AS credit_amount,
  ROUND(SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 9) AS net_cost,
  MAX(export_time) AS source_export_time
FROM `{billing_table}`
WHERE _PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end
  AND project.id = @account_id
  AND DATE(usage_start_time) >= @earliest_usage_date
GROUP BY
  account_id, billing_account_id, export_partition_date, usage_date, service_name, sku_name,
  region, author, org, repo, target_branch, resource_name, source_schema_version,
  source_allocation_scope, cluster_name, cluster_location, kubernetes_cost_class,
  kubernetes_residual_type, kubernetes_cost_component, namespace, workload_name, workload_type
ORDER BY export_partition_date, usage_date, service_name, sku_name, region, author, org, repo,
  target_branch, resource_name{limit_clause}
""".strip()


def build_gcp_unmatched_resource_query(*, billing_table: str, limit: int | None = None) -> str:
    """Return concrete resources together with their complete summary lineage.

    ``resource_name`` remains the displayable cloud resource.  The separate
    ``summary_resource_name`` deliberately follows the summary ledger's GKE
    workload/null convention, so the importer can calculate equality lineage
    without replacing the concrete identity with a nullable workload field.
    """
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    namespace = _label_expr(("k8s-namespace", "namespace"))
    workload_name = _label_expr(("k8s-workload-name",))
    workload_type = _label_expr(("k8s-workload-type",))
    cluster_name = _label_expr(("goog-k8s-cluster-name",))
    cluster_location = _label_expr(("goog-k8s-cluster-location",))
    author = _author_expr_with_overrides()
    region = _region_expr()
    is_gke = (
        f"(service.description = 'Kubernetes Engine' OR {cluster_name} IS NOT NULL "
        f"OR ({namespace} IS NOT NULL AND {workload_name} IS NOT NULL) "
        f"OR {namespace} LIKE 'kube:%' OR {namespace} LIKE 'goog-k8s-%')"
    )
    is_direct = (
        f"({namespace} IS NOT NULL AND {workload_name} IS NOT NULL "
        f"AND {namespace} NOT LIKE 'kube:%' AND {namespace} NOT LIKE 'goog-k8s-%')"
    )
    residual_type = f"""
CASE
  WHEN {namespace} = 'kube:system-overhead' THEN 'system_overhead'
  WHEN {namespace} = 'kube:unallocated' THEN 'idle'
  WHEN {namespace} = 'goog-k8s-unknown' THEN 'unknown'
  WHEN {namespace} = 'goog-k8s-unsupported-sku' THEN 'unsupported'
  WHEN service.description = 'Kubernetes Engine' AND {workload_name} IS NULL THEN 'control_plane'
  WHEN {is_gke} THEN 'unclassified'
  ELSE NULL
END
""".strip()
    cost_component = """
CASE
  WHEN service.description = 'Kubernetes Engine' THEN 'control_plane'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(core|cpu|vcpu)\\b') THEN 'cpu'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(ram|memory)\\b') THEN 'memory'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(gpu|nvidia)\\b') THEN 'gpu'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(disk|storage|hyperdisk|pd capacity)\\b') THEN 'storage'
  WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku.description, '')), r'\\b(network|egress|data transfer)\\b') THEN 'network'
  ELSE 'other'
END
""".strip()
    return f"""
WITH normalized AS (
  SELECT
    billing_account_id,
    project.id AS account_id,
    _PARTITIONDATE AS export_partition_date,
    DATE(usage_start_time) AS usage_date,
    service.description AS service_name,
    sku.description AS sku_name,
    {region} AS region,
    {namespace} AS namespace,
    {author} AS author,
    {_org_expr()} AS org,
    {_repo_expr()} AS repo,
    {_target_branch_expr()} AS target_branch,
    CASE WHEN {is_gke} THEN 'gke_cost_allocation_v1' ELSE NULL END AS source_schema_version,
    CASE WHEN {is_direct} THEN 'gke_direct' WHEN {is_gke} THEN 'gke_residual' ELSE 'direct' END
      AS source_allocation_scope,
    {cluster_name} AS cluster_name,
    {cluster_location} AS cluster_location,
    CASE WHEN {is_direct} THEN 'direct' WHEN {is_gke} THEN 'residual' ELSE NULL END
      AS kubernetes_cost_class,
    {residual_type} AS kubernetes_residual_type,
    CASE WHEN {is_gke} THEN {cost_component} ELSE NULL END AS kubernetes_cost_component,
    {workload_name} AS workload_name,
    {workload_type} AS workload_type,
    CAST(NULL AS STRING) AS owner,
    CAST(NULL AS STRING) AS service,
    CAST(NULL AS STRING) AS project,
    CAST(NULL AS STRING) AS service_exec_id,
    COALESCE(
      NULLIF(resource.name, ''),
      NULLIF(resource.global_name, ''),
      {workload_name},
      '(no GCP resource ID)'
    ) AS resource_name,
    CASE
      WHEN {is_direct} THEN {workload_name}
      WHEN {is_gke} THEN NULL
      ELSE COALESCE(NULLIF(resource.name, ''), NULLIF(resource.global_name, ''))
    END AS summary_resource_name,
    TO_JSON_STRING(
      JSON_OBJECT(
        ARRAY(SELECT label.key FROM UNNEST(labels) AS label),
        ARRAY(SELECT label.value FROM UNNEST(labels) AS label)
      )
    ) AS vendor_tags_json,
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
  account_id, billing_account_id, export_partition_date, usage_date,
  service_name, sku_name, region, namespace, author, org, repo, target_branch,
  source_schema_version, source_allocation_scope, cluster_name, cluster_location,
  kubernetes_cost_class, kubernetes_residual_type, kubernetes_cost_component,
  workload_name, workload_type, owner, service, project, service_exec_id,
  resource_name, summary_resource_name, vendor_tags_json,
  CASE
    WHEN COUNTIF(pricing_unit IS NULL OR pricing_unit NOT IN ('hour', 'minute', 'second')) > 0
      THEN NULL
    WHEN COUNTIF(pricing_unit = 'hour') = COUNT(*) THEN ROUND(SUM(amount_in_pricing_units) * 3600, 2)
    WHEN COUNTIF(pricing_unit = 'minute') = COUNT(*) THEN ROUND(SUM(amount_in_pricing_units) * 60, 2)
    WHEN COUNTIF(pricing_unit = 'second') = COUNT(*) THEN ROUND(SUM(amount_in_pricing_units), 2)
    ELSE NULL
  END AS usage_seconds,
  ROUND(SUM(cost_at_list), 9) AS list_cost,
  ROUND(SUM(cost), 9) AS effective_cost,
  ROUND(SUM(credit_amount), 9) AS credit_amount,
  ROUND(SUM(cost + credit_amount), 9) AS net_cost,
  MAX(export_time) AS source_export_time
FROM normalized
GROUP BY
  account_id, billing_account_id, export_partition_date, usage_date,
  service_name, sku_name, region, namespace, author, org, repo, target_branch,
  source_schema_version, source_allocation_scope, cluster_name, cluster_location,
  kubernetes_cost_class, kubernetes_residual_type, kubernetes_cost_component,
  workload_name, workload_type, owner, service, project, service_exec_id,
  resource_name, summary_resource_name, vendor_tags_json
ORDER BY usage_date, service_name, sku_name, resource_name{limit_clause}
""".strip()


def _label_expr(keys: Iterable[str]) -> str:
    ordered_keys = tuple(keys)
    key_list = ", ".join(repr(key) for key in ordered_keys)
    priority_cases = " ".join(
        f"WHEN {key!r} THEN {index}" for index, key in enumerate(ordered_keys)
    )
    return f"""
    ARRAY(
      SELECT label.value
      FROM UNNEST(labels) AS label
      WHERE label.key IN ({key_list})
      ORDER BY CASE label.key {priority_cases} ELSE {len(ordered_keys)} END
      LIMIT 1
    )[SAFE_OFFSET(0)]
    """.strip()


def _author_expr_with_overrides() -> str:
    label_author = _label_expr(
        (
            "k8s-label/author",
            "author",
            "k8s-label/prow.k8s.io/refs.author",
            "prow.k8s.io/refs.author",
        )
    )
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


def _org_expr() -> str:
    return _label_expr(
        (
            "k8s-label/org",
            "org",
            "k8s-label/prow.k8s.io/refs.org",
            "prow.k8s.io/refs.org",
        )
    )


def _repo_expr() -> str:
    return _label_expr(
        (
            "k8s-label/repo",
            "repo",
            "k8s-label/prow.k8s.io/refs.repo",
            "prow.k8s.io/refs.repo",
        )
    )


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
