from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any


def fetch_gcp_gke_node_cost_rows(
    *,
    billing_table: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    usage_start_date: date,
    usage_end_date: date,
    page_size: int,
) -> Iterator[dict[str, Any]]:
    """Fetch only billing rows that can be positively identified as GKE nodes."""
    from google.cloud import bigquery

    client = bigquery.Client()
    rows = client.query(
        build_gcp_gke_node_cost_query(billing_table=billing_table),
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
                bigquery.ScalarQueryParameter(
                    "export_partition_start", "DATE", export_partition_start.isoformat()
                ),
                bigquery.ScalarQueryParameter(
                    "export_partition_end", "DATE", export_partition_end.isoformat()
                ),
                bigquery.ScalarQueryParameter(
                    "usage_start_date", "DATE", usage_start_date.isoformat()
                ),
                bigquery.ScalarQueryParameter(
                    "usage_end_date", "DATE", usage_end_date.isoformat()
                ),
            ]
        ),
    ).result(page_size=page_size)
    for row in rows:
        yield dict(row.items())


def fetch_gcp_gke_workload_usage_rows(
    *,
    gke_usage_table: str,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    page_size: int,
) -> Iterator[dict[str, Any]]:
    """Fetch GKE CPU and memory metering weights for a bounded cost window."""
    from google.cloud import bigquery

    client = bigquery.Client()
    rows = client.query(
        build_gcp_gke_workload_usage_query(gke_usage_table=gke_usage_table),
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
                bigquery.ScalarQueryParameter(
                    "usage_start_date", "DATE", usage_start_date.isoformat()
                ),
                bigquery.ScalarQueryParameter(
                    "usage_end_date", "DATE", usage_end_date.isoformat()
                ),
            ]
        ),
    ).result(page_size=page_size)
    for row in rows:
        yield dict(row.items())


def build_gcp_gke_node_cost_query(*, billing_table: str) -> str:
    cluster_name_expr = _billing_label_expr("goog-k8s-cluster-name")
    cluster_location_expr = _billing_label_expr("goog-k8s-cluster-location")
    return f"""
WITH billing_rows AS (
  SELECT
    DATE(usage_start_time) AS usage_date,
    {cluster_name_expr} AS cluster_name,
    {cluster_location_expr} AS cluster_location,
    service.description AS service_name,
    sku.description AS sku_name,
    resource.name AS resource_name,
    cost_at_list
  FROM `{billing_table}`
  WHERE _PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end
    AND project.id = @account_id
    AND DATE(usage_start_time) BETWEEN @usage_start_date AND @usage_end_date
    -- CUD adjustments are not a node resource cost and are excluded from the
    -- dashboard's list-cost calculation as well.
    AND NOT STARTS_WITH(COALESCE(sku.description, ''), 'Compute Flexible Committed Use Discounts')
), gke_node_costs AS (
  SELECT
    usage_date,
    cluster_name,
    cluster_location,
    CASE
      WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku_name, '')), r'\\b(core|cpu|vcpu)\\b') THEN 'cpu'
      WHEN REGEXP_CONTAINS(LOWER(COALESCE(sku_name, '')), r'\\b(ram|memory)\\b') THEN 'memory'
      ELSE 'other'
    END AS cost_component,
    SUM(cost_at_list) AS list_cost,
    COUNT(*) AS source_row_count
  FROM billing_rows
  WHERE service_name = 'Compute Engine'
    -- Requiring both signals keeps ordinary VM spending out of the GKE pool.
    AND NULLIF(cluster_name, '') IS NOT NULL
    AND REGEXP_CONTAINS(LOWER(COALESCE(resource_name, '')), r'/instances/gke-')
  GROUP BY usage_date, cluster_name, cluster_location, cost_component
), control_plane_costs AS (
  SELECT
    usage_date,
    CAST(NULL AS STRING) AS cluster_name,
    CAST(NULL AS STRING) AS cluster_location,
    'control_plane' AS cost_component,
    SUM(cost_at_list) AS list_cost,
    COUNT(*) AS source_row_count
  FROM billing_rows
  WHERE service_name = 'Kubernetes Engine'
  GROUP BY usage_date
)
SELECT * FROM gke_node_costs
UNION ALL
SELECT * FROM control_plane_costs
ORDER BY usage_date, cluster_name, cluster_location, cost_component
""".strip()


def build_gcp_gke_workload_usage_query(*, gke_usage_table: str) -> str:
    author_expr = _metering_label_expr(("author", "prow.k8s.io/refs.author"))
    org_expr = _metering_label_expr(("org", "prow.k8s.io/refs.org"))
    repo_expr = _metering_label_expr(("repo", "prow.k8s.io/refs.repo"))
    branch_expr = _metering_label_expr(("prow.k8s.io/refs.base_ref",))
    job_name_expr = _metering_label_expr(("job-name",))
    jenkins_label_expr = _metering_label_expr(("jenkins/label",))
    prow_job_expr = _metering_label_expr(("prow.k8s.io/job",))
    app_name_expr = _metering_label_expr(("app.kubernetes.io/name", "app", "k8s-app"))
    return f"""
WITH metering_rows AS (
  SELECT
    DATE(start_time) AS usage_date,
    NULLIF(cluster_name, '') AS cluster_name,
    NULLIF(cluster_location, '') AS cluster_location,
    NULLIF(namespace, '') AS namespace,
    {author_expr} AS author,
    {org_expr} AS org,
    {repo_expr} AS repo,
    {branch_expr} AS target_branch,
    {job_name_expr} AS job_name,
    {jenkins_label_expr} AS jenkins_label,
    {prow_job_expr} AS prow_job,
    {app_name_expr} AS app_name,
    resource_name,
    usage.amount AS usage_amount
  FROM `{gke_usage_table}`
  WHERE project.id = @account_id
    AND DATE(start_time) BETWEEN @usage_start_date AND @usage_end_date
    AND resource_name IN ('cpu', 'memory')
), workloads AS (
  SELECT
    usage_date,
    cluster_name,
    cluster_location,
    namespace,
    author,
    org,
    repo,
    target_branch,
    COALESCE(job_name, jenkins_label, prow_job, app_name, namespace) AS workload_name,
    CASE
      WHEN job_name IS NOT NULL THEN 'Job'
      WHEN jenkins_label IS NOT NULL THEN 'Jenkins agent'
      WHEN prow_job IS NOT NULL THEN 'Prow job'
      WHEN app_name IS NOT NULL THEN 'Application'
      ELSE 'Namespace'
    END AS workload_type,
    resource_name,
    usage_amount
  FROM metering_rows
  WHERE cluster_name IS NOT NULL
    AND cluster_location IS NOT NULL
    AND namespace IS NOT NULL
)
SELECT
  usage_date,
  cluster_name,
  cluster_location,
  namespace,
  workload_name,
  workload_type,
  author,
  org,
  repo,
  target_branch,
  SUM(IF(resource_name = 'cpu', usage_amount, 0)) AS cpu_seconds,
  SUM(IF(resource_name = 'memory', usage_amount, 0)) AS memory_byte_seconds
FROM workloads
GROUP BY
  usage_date,
  cluster_name,
  cluster_location,
  namespace,
  workload_name,
  workload_type,
  author,
  org,
  repo,
  target_branch
HAVING cpu_seconds > 0 OR memory_byte_seconds > 0
ORDER BY usage_date, cluster_name, cluster_location, namespace, workload_name
""".strip()


def _billing_label_expr(key: str) -> str:
    return f"""
ARRAY(
  SELECT label.value
  FROM UNNEST(labels) AS label
  WHERE label.key = '{key}' AND NULLIF(label.value, '') IS NOT NULL
  LIMIT 1
)[SAFE_OFFSET(0)]
""".strip()


def _metering_label_expr(keys: tuple[str, ...]) -> str:
    key_list = ", ".join(repr(key) for key in keys)
    priority = " ".join(
        f"WHEN {key!r} THEN {index}" for index, key in enumerate(keys)
    )
    return f"""
ARRAY(
  SELECT label.value
  FROM UNNEST(labels) AS label
  WHERE label.key IN ({key_list}) AND NULLIF(label.value, '') IS NOT NULL
  ORDER BY CASE label.key {priority} ELSE {len(keys)} END
  LIMIT 1
)[SAFE_OFFSET(0)]
""".strip()
