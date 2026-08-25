from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from typing import Any

_BIGQUERY_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_AWS_CE_UNBLENDED_LINE_ITEM_TYPES = "'Usage', 'SavingsPlanCoveredUsage'"


def fetch_aws_split_cost_summary_rows(
    *,
    billing_table: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    earliest_usage_date: date,
    usage_end_date: date | None = None,
    page_size: int,
    limit: int | None = None,
    validate_guardrail: bool = True,
) -> Iterator[dict[str, Any]]:
    yield from _fetch_rows(
        query=build_aws_split_cost_summary_query(
            billing_table=billing_table,
            limit=limit,
            include_usage_end_date=usage_end_date is not None,
        ),
        guardrail_query=(
            build_aws_split_cost_guardrail_query(
                billing_table=billing_table,
                include_usage_end_date=usage_end_date is not None,
            )
            if validate_guardrail
            else None
        ),
        account_id=account_id,
        export_partition_start=export_partition_start,
        export_partition_end=export_partition_end,
        earliest_usage_date=earliest_usage_date,
        usage_end_date=usage_end_date,
        page_size=page_size,
    )


def fetch_aws_split_cost_unmatched_resource_rows(
    *,
    billing_table: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    usage_start_date: date,
    usage_end_date: date,
    page_size: int,
    limit: int | None = None,
    validate_guardrail: bool = True,
) -> Iterator[dict[str, Any]]:
    yield from _fetch_rows(
        query=build_aws_split_cost_unmatched_resource_query(
            billing_table=billing_table,
            limit=limit,
        ),
        guardrail_query=(
            build_aws_split_cost_guardrail_query(
                billing_table=billing_table,
                include_usage_end_date=True,
            )
            if validate_guardrail
            else None
        ),
        account_id=account_id,
        export_partition_start=export_partition_start,
        export_partition_end=export_partition_end,
        earliest_usage_date=usage_start_date,
        usage_end_date=usage_end_date,
        page_size=page_size,
    )


def fetch_aws_split_cost_parent_residual_allocation_rows(
    *,
    billing_table: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    earliest_usage_date: date,
    usage_end_date: date | None,
    page_size: int,
    validate_guardrail: bool = True,
) -> Iterator[dict[str, Any]]:
    yield from _fetch_rows(
        query=build_aws_split_cost_parent_residual_allocation_query(
            billing_table=billing_table,
            include_usage_end_date=usage_end_date is not None,
        ),
        guardrail_query=(
            build_aws_split_cost_guardrail_query(
                billing_table=billing_table,
                include_usage_end_date=usage_end_date is not None,
            )
            if validate_guardrail
            else None
        ),
        account_id=account_id,
        export_partition_start=export_partition_start,
        export_partition_end=export_partition_end,
        earliest_usage_date=earliest_usage_date,
        usage_end_date=usage_end_date,
        page_size=page_size,
    )


def _fetch_rows(
    *,
    query: str,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    earliest_usage_date: date,
    page_size: int,
    usage_end_date: date | None = None,
    guardrail_query: str | None = None,
) -> Iterator[dict[str, Any]]:
    from google.cloud import bigquery

    parameters = [
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
    if usage_end_date is not None:
        parameters.append(
            bigquery.ScalarQueryParameter("usage_end_date", "DATE", usage_end_date.isoformat())
        )
    client = bigquery.Client()
    if guardrail_query is not None:
        violations = list(
            client.query(
                guardrail_query,
                job_config=bigquery.QueryJobConfig(query_parameters=parameters),
            ).result(page_size=100)
        )
        if violations:
            details = [dict(row.items()) for row in violations]
            raise ValueError(
                "AWS split-cost child allocation exceeds parent direct cost: "
                f"{details!r}"
            )
    rows = client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=parameters)).result(
        page_size=page_size
    )
    for row in rows:
        yield dict(row.items())


def build_aws_split_cost_summary_query(
    *,
    billing_table: str,
    limit: int | None = None,
    include_usage_end_date: bool = False,
) -> str:
    return _build_split_cost_query(
        billing_table=billing_table,
        resource_level=False,
        include_usage_end_date=include_usage_end_date,
        limit=limit,
    )


def build_aws_split_cost_unmatched_resource_query(
    *,
    billing_table: str,
    limit: int | None = None,
) -> str:
    return _build_split_cost_query(
        billing_table=billing_table,
        resource_level=True,
        include_usage_end_date=True,
        limit=limit,
    )


def build_aws_split_cost_guardrail_query(
    *,
    billing_table: str,
    include_usage_end_date: bool = False,
) -> str:
    """Return parent/day violations before an import can write any normalized facts."""
    table = _quote_bigquery_table(billing_table)
    usage_end_filter = (
        "\n    AND DATE(line_item_usage_start_date) <= @usage_end_date"
        if include_usage_end_date
        else ""
    )
    return f"""
WITH raw AS (
  SELECT
    line_item_usage_account_id AS account_id,
    DATE(line_item_usage_start_date) AS usage_date,
    NULLIF(line_item_resource_id, '') AS resource_name,
    NULLIF(split_line_item_parent_resource_id, '') AS parent_resource_name,
    CASE
      WHEN line_item_line_item_type IN ({_AWS_CE_UNBLENDED_LINE_ITEM_TYPES})
        THEN COALESCE(line_item_unblended_cost, 0)
      ELSE 0
    END AS direct_list_cost,
    COALESCE(line_item_unblended_cost, 0) AS direct_effective_cost,
    CASE
      WHEN line_item_line_item_type IN ({_AWS_CE_UNBLENDED_LINE_ITEM_TYPES})
        THEN COALESCE(split_line_item_split_cost, 0)
      ELSE 0
    END AS split_list_cost,
    COALESCE(split_line_item_split_cost, 0) AS split_effective_cost
  FROM {table}
  WHERE line_item_usage_account_id = @account_id
    AND DATE(bill_billing_period_start_date) BETWEEN @export_partition_start AND @export_partition_end
    AND DATE(line_item_usage_start_date) >= @earliest_usage_date{usage_end_filter}
),
parent_keys AS (
  SELECT DISTINCT account_id, usage_date, parent_resource_name
  FROM raw
  WHERE parent_resource_name IS NOT NULL
),
parent_direct AS (
  SELECT
    raw.account_id,
    raw.usage_date,
    raw.resource_name AS parent_resource_name,
    SUM(raw.direct_list_cost) AS parent_direct_list_cost,
    SUM(raw.direct_effective_cost) AS parent_direct_effective_cost
  FROM raw
  JOIN parent_keys
    ON parent_keys.account_id = raw.account_id
   AND parent_keys.usage_date = raw.usage_date
   AND parent_keys.parent_resource_name = raw.resource_name
  WHERE raw.parent_resource_name IS NULL
  GROUP BY raw.account_id, raw.usage_date, raw.resource_name
),
child_split AS (
  SELECT
    account_id,
    usage_date,
    parent_resource_name,
    SUM(split_list_cost) AS child_split_list_cost,
    SUM(split_effective_cost) AS child_split_effective_cost
  FROM raw
  WHERE parent_resource_name IS NOT NULL
  GROUP BY account_id, usage_date, parent_resource_name
)
SELECT
  child.account_id,
  child.usage_date,
  child.parent_resource_name,
  COALESCE(parent.parent_direct_list_cost, 0) AS parent_direct_list_cost,
  child.child_split_list_cost,
  COALESCE(parent.parent_direct_effective_cost, 0) AS parent_direct_effective_cost,
  child.child_split_effective_cost
FROM child_split AS child
LEFT JOIN parent_direct AS parent
  ON parent.account_id = child.account_id
 AND parent.usage_date = child.usage_date
 AND parent.parent_resource_name = child.parent_resource_name
WHERE child.child_split_list_cost - COALESCE(parent.parent_direct_list_cost, 0) > 0.01
   OR child.child_split_effective_cost - COALESCE(parent.parent_direct_effective_cost, 0) > 0.01
ORDER BY child.usage_date, child.parent_resource_name
LIMIT 100
""".strip()


def build_aws_split_cost_parent_residual_allocation_query(
    *,
    billing_table: str,
    include_usage_end_date: bool = False,
) -> str:
    """Return the parent/pod grain retained only by the residual audit ledger."""
    table = _quote_bigquery_table(billing_table)
    usage_end_filter = (
        "\n    AND DATE(line_item_usage_start_date) <= @usage_end_date"
        if include_usage_end_date
        else ""
    )
    return f"""
WITH raw AS (
  SELECT
    line_item_usage_account_id AS account_id,
    DATE(line_item_usage_start_date) AS usage_date,
    NULLIF(line_item_resource_id, '') AS resource_name,
    NULLIF(split_line_item_parent_resource_id, '') AS parent_resource_name,
    NULLIF(TRIM(resource_tags_aws_eks_namespace), '') AS namespace,
    NULLIF(TRIM(resource_tags_aws_eks_workload_name), '') AS workload_name,
    NULLIF(TRIM(resource_tags_aws_eks_workload_type), '') AS workload_type,
    NULLIF(TRIM(resource_tags_user_icost_owner_email), '') AS owner,
    NULLIF(TRIM(resource_tags_user_icost_service), '') AS service,
    NULLIF(TRIM(COALESCE(resource_tags_user_icost_project, resource_tags_user_project)), '') AS project,
    NULLIF(TRIM(resource_tags_user_icost_service_exec_id), '') AS service_exec_id,
    CASE
      WHEN line_item_line_item_type IN ({_AWS_CE_UNBLENDED_LINE_ITEM_TYPES})
        THEN COALESCE(line_item_unblended_cost, 0)
      ELSE 0
    END AS direct_list_cost,
    CASE
      WHEN line_item_line_item_type IN ({_AWS_CE_UNBLENDED_LINE_ITEM_TYPES})
        THEN COALESCE(split_line_item_split_cost, 0)
      ELSE 0
    END AS split_list_cost
  FROM {table}
  WHERE line_item_usage_account_id = @account_id
    AND DATE(bill_billing_period_start_date) BETWEEN @export_partition_start AND @export_partition_end
    AND DATE(line_item_usage_start_date) >= @earliest_usage_date{usage_end_filter}
),
parent_keys AS (
  SELECT DISTINCT account_id, usage_date, parent_resource_name
  FROM raw
  WHERE parent_resource_name IS NOT NULL
),
parent_direct AS (
  SELECT
    raw.account_id,
    raw.usage_date,
    raw.resource_name AS parent_resource_id,
    SUM(raw.direct_list_cost) AS parent_direct_list_cost
  FROM raw
  JOIN parent_keys
    ON parent_keys.account_id = raw.account_id
   AND parent_keys.usage_date = raw.usage_date
   AND parent_keys.parent_resource_name = raw.resource_name
  WHERE raw.parent_resource_name IS NULL
  GROUP BY raw.account_id, raw.usage_date, raw.resource_name
),
all_child_split AS (
  SELECT
    account_id,
    usage_date,
    parent_resource_name,
    SUM(split_list_cost) AS all_child_split_list_cost
  FROM raw
  WHERE parent_resource_name IS NOT NULL
  GROUP BY account_id, usage_date, parent_resource_name
),
pod_split AS (
  SELECT
    account_id,
    usage_date,
    parent_resource_name,
    COALESCE(
      resource_name,
      CONCAT(
        'eks-tag:',
        COALESCE(namespace, ''), ':',
        COALESCE(workload_name, ''), ':',
        COALESCE(workload_type, '')
      )
    ) AS pod_resource_id,
    ANY_VALUE(namespace) AS namespace,
    ANY_VALUE(workload_name) AS workload_name,
    ANY_VALUE(workload_type) AS workload_type,
    ANY_VALUE(owner) AS owner,
    ANY_VALUE(service) AS service,
    ANY_VALUE(project) AS project,
    ANY_VALUE(service_exec_id) AS service_exec_id,
    SUM(split_list_cost) AS source_pod_split_list_cost
  FROM raw
  WHERE parent_resource_name IS NOT NULL
    AND (
      REGEXP_CONTAINS(LOWER(COALESCE(resource_name, '')), r'(^|:)pod/')
      OR namespace IS NOT NULL
    )
  GROUP BY
    account_id,
    usage_date,
    parent_resource_name,
    COALESCE(
      resource_name,
      CONCAT(
        'eks-tag:',
        COALESCE(namespace, ''), ':',
        COALESCE(workload_name, ''), ':',
        COALESCE(workload_type, '')
      )
    )
)
SELECT
  'aws' AS vendor,
  parent.usage_date,
  parent.account_id,
  parent.parent_resource_id,
  pod.pod_resource_id,
  pod.namespace,
  pod.workload_name,
  pod.workload_type,
  pod.owner,
  pod.service,
  pod.project,
  pod.service_exec_id,
  pod.source_pod_split_list_cost,
  parent.parent_direct_list_cost,
  CASE
    WHEN parent.parent_direct_list_cost - all_child.all_child_split_list_cost BETWEEN -0.01 AND 0
      THEN CAST(0 AS NUMERIC)
    ELSE parent.parent_direct_list_cost - all_child.all_child_split_list_cost
  END AS parent_residual_list_cost
FROM parent_direct AS parent
JOIN all_child_split AS all_child
  ON all_child.account_id = parent.account_id
 AND all_child.usage_date = parent.usage_date
 AND all_child.parent_resource_name = parent.parent_resource_id
JOIN pod_split AS pod
  ON pod.account_id = parent.account_id
 AND pod.usage_date = parent.usage_date
 AND pod.parent_resource_name = parent.parent_resource_id
ORDER BY parent.usage_date, parent.parent_resource_id, pod.pod_resource_id
""".strip()


def _build_split_cost_query(
    *,
    billing_table: str,
    resource_level: bool,
    include_usage_end_date: bool,
    limit: int | None,
) -> str:
    table = _quote_bigquery_table(billing_table)
    usage_end_filter = "\n    AND DATE(line_item_usage_start_date) <= @usage_end_date" if include_usage_end_date else ""
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    resource_columns = """
  resource_name,
  parent_resource_name,
  CASE
    WHEN COUNTIF(pricing_unit IS NULL OR pricing_unit NOT IN ('hour', 'minute', 'second')) > 0
      THEN NULL
    WHEN COUNTIF(pricing_unit = 'hour') = COUNT(*) THEN SUM(usage_amount) * 3600
    WHEN COUNTIF(pricing_unit = 'minute') = COUNT(*) THEN SUM(usage_amount) * 60
    WHEN COUNTIF(pricing_unit = 'second') = COUNT(*) THEN SUM(usage_amount)
    ELSE NULL
  END AS usage_seconds,"""
    resource_grouping = """,
  resource_name,
  parent_resource_name"""
    resource_ordering = ", resource_name"
    resource_name_filter = "\n  AND resource_name IS NOT NULL"
    if not resource_level:
        resource_columns = ""
        resource_grouping = ""
        resource_ordering = ""
        resource_name_filter = ""

    return f"""
WITH raw AS (
  SELECT
    line_item_usage_account_id AS account_id,
    NULLIF(bill_payer_account_id, '') AS billing_account_id,
    DATE(bill_billing_period_start_date) AS export_partition_date,
    DATE(line_item_usage_start_date) AS usage_date,
    COALESCE(
      NULLIF(line_item_resource_id, ''),
      NULLIF(line_item_line_item_description, '')
    ) AS resource_name,
    NULLIF(split_line_item_parent_resource_id, '') AS parent_resource_name,
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
    NULLIF(TRIM(resource_tags_user_icost_owner_email), '') AS owner,
    NULLIF(TRIM(resource_tags_user_icost_service), '') AS service,
    NULLIF(TRIM(COALESCE(resource_tags_user_icost_project, resource_tags_user_project)), '') AS project,
    NULLIF(TRIM(resource_tags_user_icost_service_exec_id), '') AS service_exec_id,
    NULLIF(TRIM(resource_tags_user_usedby), '') AS author_fallback,
    NULLIF(TRIM(resource_tags_user_tenant), '') AS org,
    NULLIF(TRIM(resource_tags_user_cluster), '') AS cluster,
    NULLIF(TRIM(resource_tags_user_shared_pool), '') AS shared_pool,
    NULLIF(TRIM(resource_tags_aws_eks_namespace), '') AS namespace,
    NULLIF(TRIM(resource_tags_aws_eks_workload_name), '') AS workload_name,
    NULLIF(TRIM(resource_tags_aws_eks_workload_type), '') AS workload_type,
    LOWER(NULLIF(pricing_unit, '')) AS pricing_unit,
    COALESCE(line_item_usage_amount, 0) AS usage_amount,
    COALESCE(split_line_item_split_usage, 0) AS split_usage_amount,
    CASE
      WHEN line_item_line_item_type IN ({_AWS_CE_UNBLENDED_LINE_ITEM_TYPES})
        THEN COALESCE(line_item_unblended_cost, 0)
      ELSE 0
    END AS direct_list_cost,
    COALESCE(line_item_unblended_cost, 0) AS direct_effective_cost,
    CASE
      WHEN line_item_line_item_type IN ({_AWS_CE_UNBLENDED_LINE_ITEM_TYPES})
        THEN COALESCE(split_line_item_split_cost, 0)
      ELSE 0
    END AS split_list_cost,
    COALESCE(split_line_item_split_cost, 0) AS split_effective_cost,
    line_item_usage_end_date AS source_export_time
  FROM {table}
  WHERE line_item_usage_account_id = @account_id
    AND DATE(bill_billing_period_start_date) BETWEEN @export_partition_start AND @export_partition_end
    AND DATE(line_item_usage_start_date) >= @earliest_usage_date{usage_end_filter}
),
parent_keys AS (
  SELECT DISTINCT account_id, usage_date, parent_resource_name
  FROM raw
  WHERE parent_resource_name IS NOT NULL
),
eks_parent_tags AS (
  SELECT DISTINCT
    parent.shared_pool,
    parent.cluster
  FROM raw AS parent
  JOIN parent_keys
    ON parent_keys.account_id = parent.account_id
   AND parent_keys.usage_date = parent.usage_date
   AND parent_keys.parent_resource_name = parent.resource_name
  WHERE parent.parent_resource_name IS NULL
    AND (parent.shared_pool IS NOT NULL OR parent.cluster IS NOT NULL)
),
parent_direct AS (
  SELECT
    raw.account_id,
    raw.usage_date,
    raw.resource_name AS parent_resource_name,
    ANY_VALUE(raw.billing_account_id) AS billing_account_id,
    ANY_VALUE(raw.export_partition_date) AS export_partition_date,
    ANY_VALUE(raw.service_name) AS service_name,
    ANY_VALUE(raw.sku_name) AS sku_name,
    ANY_VALUE(raw.usage_type) AS usage_type,
    ANY_VALUE(raw.region) AS region,
    ANY_VALUE(raw.owner) AS owner,
    ANY_VALUE(raw.service) AS service,
    ANY_VALUE(raw.project) AS project,
    ANY_VALUE(raw.service_exec_id) AS service_exec_id,
    ANY_VALUE(raw.author_fallback) AS author_fallback,
    ANY_VALUE(raw.org) AS org,
    ANY_VALUE(raw.cluster) AS cluster,
    ANY_VALUE(raw.shared_pool) AS shared_pool,
    ANY_VALUE(raw.pricing_unit) AS pricing_unit,
    SUM(raw.usage_amount) AS usage_amount,
    SUM(raw.direct_list_cost) AS direct_list_cost,
    SUM(raw.direct_effective_cost) AS direct_effective_cost,
    MAX(raw.source_export_time) AS source_export_time
  FROM raw
  JOIN parent_keys
    ON parent_keys.account_id = raw.account_id
   AND parent_keys.usage_date = raw.usage_date
   AND parent_keys.parent_resource_name = raw.resource_name
  WHERE raw.parent_resource_name IS NULL
  GROUP BY raw.account_id, raw.usage_date, raw.resource_name
),
child_split AS (
  SELECT
    raw.account_id,
    raw.usage_date,
    raw.parent_resource_name,
    raw.resource_name,
    raw.owner,
    raw.service,
    raw.project,
    raw.service_exec_id,
    raw.author_fallback,
    raw.org,
    raw.cluster,
    raw.shared_pool,
    raw.namespace,
    raw.workload_name,
    raw.workload_type,
    SUM(raw.split_usage_amount) AS split_usage_amount,
    SUM(raw.split_list_cost) AS split_list_cost,
    SUM(raw.split_effective_cost) AS split_effective_cost,
    MAX(raw.source_export_time) AS source_export_time
  FROM raw
  WHERE raw.parent_resource_name IS NOT NULL
  GROUP BY
    raw.account_id,
    raw.usage_date,
    raw.parent_resource_name,
    raw.resource_name,
    raw.owner,
    raw.service,
    raw.project,
    raw.service_exec_id,
    raw.author_fallback,
    raw.org,
    raw.cluster,
    raw.shared_pool,
    raw.namespace,
    raw.workload_name,
    raw.workload_type
),
branch_rows AS (
  SELECT
    raw.account_id,
    raw.billing_account_id,
    raw.export_partition_date,
    raw.usage_date,
    raw.service_name,
    raw.sku_name,
    raw.usage_type,
    raw.region,
    raw.resource_name,
    CAST(NULL AS STRING) AS parent_resource_name,
    CAST(NULL AS STRING) AS namespace,
    CAST(NULL AS STRING) AS workload_name,
    CAST(NULL AS STRING) AS workload_type,
    raw.owner,
    raw.service,
    raw.project,
    raw.service_exec_id,
    COALESCE(raw.owner, raw.author_fallback) AS author,
    raw.org,
    raw.pricing_unit,
    raw.usage_amount AS usage_amount,
    raw.cluster,
    raw.shared_pool,
    CASE
      -- EBS volumes are the billing representation of EKS PVCs. Only retain
      -- volumes with an explicit cluster/shared-pool signal as Kubernetes;
      -- untagged volumes remain ordinary direct cost.
      WHEN raw.service_name = 'AmazonEC2'
        AND STARTS_WITH(LOWER(COALESCE(raw.resource_name, '')), 'vol-')
        AND (
          raw.cluster IS NOT NULL
          OR EXISTS (
            SELECT 1
            FROM eks_parent_tags
            WHERE eks_parent_tags.shared_pool IS NOT NULL
              AND eks_parent_tags.shared_pool = raw.shared_pool
          )
        )
        THEN 'eks_unallocated'
      ELSE 'direct'
    END AS source_allocation_scope,
    raw.direct_list_cost AS list_cost,
    raw.direct_effective_cost AS effective_cost,
    raw.direct_effective_cost AS net_cost,
    raw.source_export_time
  FROM raw
  LEFT JOIN parent_keys
    ON parent_keys.account_id = raw.account_id
   AND parent_keys.usage_date = raw.usage_date
   AND parent_keys.parent_resource_name = raw.resource_name
  WHERE raw.parent_resource_name IS NULL
    AND parent_keys.parent_resource_name IS NULL

  UNION ALL

  SELECT
    parent.account_id,
    parent.billing_account_id,
    parent.export_partition_date,
    parent.usage_date,
    parent.service_name,
    'EKS:ParentResidual' AS sku_name,
    'EKS:ParentResidual' AS usage_type,
    parent.region,
    parent.parent_resource_name AS resource_name,
    CAST(NULL AS STRING) AS parent_resource_name,
    CAST(NULL AS STRING) AS namespace,
    CAST(NULL AS STRING) AS workload_name,
    CAST(NULL AS STRING) AS workload_type,
    parent.owner,
    parent.service,
    parent.project,
    parent.service_exec_id,
    COALESCE(parent.owner, parent.author_fallback) AS author,
    parent.org,
    parent.pricing_unit,
    parent.usage_amount AS usage_amount,
    parent.cluster,
    parent.shared_pool,
    'eks_parent_residual' AS source_allocation_scope,
    CASE
      WHEN parent.direct_list_cost - COALESCE(SUM(child.split_list_cost), 0) BETWEEN -0.01 AND 0
        THEN CAST(0 AS NUMERIC)
      ELSE parent.direct_list_cost - COALESCE(SUM(child.split_list_cost), 0)
    END AS list_cost,
    CASE
      WHEN parent.direct_effective_cost - COALESCE(SUM(child.split_effective_cost), 0) BETWEEN -0.01 AND 0
        THEN CAST(0 AS NUMERIC)
      ELSE parent.direct_effective_cost - COALESCE(SUM(child.split_effective_cost), 0)
    END AS effective_cost,
    CASE
      WHEN parent.direct_effective_cost - COALESCE(SUM(child.split_effective_cost), 0) BETWEEN -0.01 AND 0
        THEN CAST(0 AS NUMERIC)
      ELSE parent.direct_effective_cost - COALESCE(SUM(child.split_effective_cost), 0)
    END AS net_cost,
    GREATEST(
      parent.source_export_time,
      COALESCE(MAX(child.source_export_time), parent.source_export_time)
    ) AS source_export_time
  FROM parent_direct AS parent
  LEFT JOIN child_split AS child
    ON child.account_id = parent.account_id
   AND child.usage_date = parent.usage_date
   AND child.parent_resource_name = parent.parent_resource_name
  GROUP BY
    parent.account_id,
    parent.billing_account_id,
    parent.export_partition_date,
    parent.usage_date,
    parent.service_name,
    parent.region,
    parent.parent_resource_name,
    parent.owner,
    parent.service,
    parent.project,
    parent.service_exec_id,
    parent.author_fallback,
    parent.org,
    parent.pricing_unit,
    parent.usage_amount,
    parent.cluster,
    parent.shared_pool,
    parent.direct_list_cost,
    parent.direct_effective_cost,
    parent.source_export_time

  UNION ALL

  SELECT
    parent.account_id,
    parent.billing_account_id,
    parent.export_partition_date,
    parent.usage_date,
    parent.service_name,
    parent.sku_name,
    parent.usage_type,
    parent.region,
    child.resource_name,
    child.parent_resource_name,
    child.namespace,
    child.workload_name,
    child.workload_type,
    child.owner,
    child.service,
    child.project,
    child.service_exec_id,
    COALESCE(child.owner, child.author_fallback) AS author,
    child.org,
    parent.pricing_unit,
    child.split_usage_amount AS usage_amount,
    -- AWS split children can omit either routing tag; retain their own value
    -- and inherit only the missing tag from the matched parent for TCMS routing.
    COALESCE(child.cluster, parent.cluster) AS cluster,
    COALESCE(child.shared_pool, parent.shared_pool) AS shared_pool,
    CASE
      -- Some split-cost exports identify a workload through the EKS namespace
      -- allocation tag rather than a pod ARN. Both are direct EKS evidence.
      WHEN REGEXP_CONTAINS(LOWER(COALESCE(child.resource_name, '')), r'(^|:)pod/')
        OR child.namespace IS NOT NULL
        THEN 'eks_pod'
      ELSE 'split_child'
    END AS source_allocation_scope,
    child.split_list_cost AS list_cost,
    child.split_effective_cost AS effective_cost,
    child.split_effective_cost AS net_cost,
    GREATEST(parent.source_export_time, child.source_export_time) AS source_export_time
  FROM child_split AS child
  JOIN parent_direct AS parent
    ON parent.account_id = child.account_id
   AND parent.usage_date = child.usage_date
   AND parent.parent_resource_name = child.parent_resource_name
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
  source_allocation_scope,
  namespace,
  workload_name,
  workload_type,
  owner,
  service,
  project,
  service_exec_id,
  author,
  org,
  project AS repo,
  CASE
    WHEN shared_pool IS NULL AND cluster IS NULL THEN NULL
    ELSE TO_JSON_STRING(STRUCT(cluster AS cluster, shared_pool AS shared_pool))
  END AS vendor_tags_json,
  {resource_columns}
  SUM(list_cost) AS list_cost,
  SUM(effective_cost) AS effective_cost,
  CAST(0 AS NUMERIC) AS credit_amount,
  SUM(net_cost) AS net_cost,
  MAX(source_export_time) AS source_export_time
FROM branch_rows
WHERE (list_cost != 0 OR effective_cost != 0 OR net_cost != 0){resource_name_filter}
GROUP BY
  account_id,
  billing_account_id,
  export_partition_date,
  usage_date,
  service_name,
  sku_name,
  region,
  source_allocation_scope,
  namespace,
  workload_name,
  workload_type,
  owner,
  service,
  project,
  service_exec_id,
  author,
  org,
  cluster,
  shared_pool{resource_grouping}
ORDER BY usage_date, service_name, sku_name, source_allocation_scope{resource_ordering}{limit_clause}
""".strip()


def _quote_bigquery_table(table: str) -> str:
    if not _BIGQUERY_TABLE_RE.fullmatch(table):
        raise ValueError(f"Invalid BigQuery table identifier: {table!r}")
    return f"`{table}`"
