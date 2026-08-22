from decimal import Decimal

from cost_insight.sources.gcp_billing_export import (
    _region_expr,
    build_gcp_billing_summary_query,
    build_gcp_unmatched_resource_query,
    decimal_or_none,
)


def _assert_target_branch_label_keys(query: str) -> None:
    assert "'k8s-label/prow.k8s.io/refs.base_ref'" in query
    assert "'prow.k8s.io/refs.base_ref'" in query


def _assert_prow_ref_label_keys(query: str) -> None:
    assert "'k8s-label/prow.k8s.io/refs.author'" in query
    assert "'prow.k8s.io/refs.author'" in query
    assert "'k8s-label/prow.k8s.io/refs.org'" in query
    assert "'prow.k8s.io/refs.org'" in query
    assert "'k8s-label/prow.k8s.io/refs.repo'" in query
    assert "'prow.k8s.io/refs.repo'" in query


def _assert_region_bucket_expr(query: str) -> None:
    region_expr = _region_expr()

    assert f"{region_expr} AS region" in query
    assert "location.region AS region" not in query
    assert "service.description" not in region_expr
    assert "artifact registry" not in region_expr.lower()
    assert "bigquery" not in region_expr.lower()
    assert "cloud logging" not in region_expr.lower()
    assert "'multi-region'" in query
    assert "'cross-region'" in query
    assert "'global'" in query
    assert "'unknown'" in query


def test_region_expr_keeps_region_bucket_branch_order() -> None:
    expr = _region_expr()

    region_idx = expr.index("NULLIF(location.region, '') IS NOT NULL")
    location_multi_idx = expr.index("LOWER(COALESCE(location.location, '')) IN")
    sku_multi_idx = expr.index(r"r'\b(multi[- ]region|dual[- ]region)\b'")
    sku_cross_idx = expr.index(r"r'\b(inter[- ]region|cross[- ]region|replication)\b'")
    transfer_idx = expr.index(r"r'\b(data transfer|egress)\b.*\b(to|from|within)\b'")
    location_global_idx = expr.index("LOWER(COALESCE(location.location, '')) = 'global'")

    assert (
        region_idx
        < location_multi_idx
        < sku_multi_idx
        < sku_cross_idx
        < transfer_idx
        < location_global_idx
    )


def test_build_gcp_billing_summary_query_uses_partition_pruning() -> None:
    query = build_gcp_billing_summary_query(billing_table="project.dataset.table", limit=20)

    assert "`project.dataset.table`" in query
    assert "_PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end" in query
    assert "DATE(usage_start_time) >= @earliest_usage_date" in query
    assert "k8s-label/author" in query
    assert "k8s-label/repo" in query
    _assert_target_branch_label_keys(query)
    _assert_prow_ref_label_keys(query)
    assert "target_branch" in query
    assert "resource_name" in query
    assert "NULLIF(resource.name, '')" in query
    assert "NULLIF(resource.global_name, '')" in query
    assert query.index("k8s-workload-name") < query.index("NULLIF(resource.name, '')")
    assert "service.description AS service_name" in query
    assert "sku.description AS sku_name" in query
    _assert_region_bucket_expr(query)
    assert "NULL AS usage_type" in query
    assert "Cloud Logging" in query
    assert "Compute Flexible Committed Use Discounts - 3 Year" in query
    assert "Compute Flexible Committed Use Discounts - 1 Year" in query
    assert "wei_zheng" in query
    assert "LIMIT 20" in query


def test_gcp_summary_uses_workload_identity_instead_of_gke_resource_ids() -> None:
    query = build_gcp_billing_summary_query(billing_table="project.dataset.table")

    resource_case = query[
        query.index("AS target_branch,") + len("AS target_branch,") : query.index("AS resource_name")
    ]
    assert "THEN NULL" in resource_case
    assert resource_case.index("k8s-workload-name") < resource_case.index("THEN NULL")
    assert resource_case.index("THEN NULL") < resource_case.index("NULLIF(resource.name, '')")


def test_build_gcp_unmatched_resource_query_preserves_native_resource_name_and_labels() -> None:
    query = build_gcp_unmatched_resource_query(billing_table="project.dataset.billing")

    assert "TO_JSON_STRING(" in query
    assert "JSON_OBJECT(" in query
    assert "AS vendor_tags_json" in query
    assert query.index("NULLIF(resource.name, '')") < query.index("k8s-workload-name")
    assert "'(no GCP resource ID)'" in query


def test_build_gcp_unmatched_resource_query_keeps_resource_context() -> None:
    query = build_gcp_unmatched_resource_query(billing_table="project.dataset.table")

    assert "_PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end" in query
    assert "DATE(usage_start_time) BETWEEN @usage_start_date AND @usage_end_date" in query
    assert "k8s-workload-name" in query
    _assert_target_branch_label_keys(query)
    assert "target_branch" in query
    assert "resource.global_name" in query
    assert "usage_seconds" in query
    assert "service.description AS service_name" in query
    assert "Cloud Logging" in query
    assert "wei_zheng" in query


def test_decimal_or_none() -> None:
    value = Decimal("1.23")

    assert decimal_or_none(None) is None
    assert decimal_or_none(value) is value
    assert decimal_or_none("2.34") == Decimal("2.34")
