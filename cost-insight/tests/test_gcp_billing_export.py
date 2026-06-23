import sys
import types
from datetime import date
from decimal import Decimal

from cost_insight.sources.gcp_billing_export import (
    build_gcp_billing_query,
    build_gcp_billing_summary_query,
    build_gcp_unmatched_resource_query,
    decimal_or_none,
    fetch_gcp_billing_rows,
)


def _assert_target_branch_label_keys(query: str) -> None:
    assert "'k8s-label/prow.k8s.io/refs.base_ref'" in query
    assert "'prow.k8s.io/refs.base_ref'" in query


def test_build_gcp_billing_query_keeps_expected_dimensions() -> None:
    query = build_gcp_billing_query(billing_table="project.dataset.table", limit=10)

    assert "`project.dataset.table`" in query
    assert "k8s-label/author" in query
    assert "k8s-label/repo" in query
    _assert_target_branch_label_keys(query)
    assert "target_branch" in query
    assert "k8s-workload-name" in query
    assert "cost_at_list" in query
    assert "pricing_unit NOT IN ('hour', 'minute', 'second')" in query
    assert "ROUND(SUM(amount_in_pricing_units) * 60, 2)" in query
    assert "Cloud Logging" in query
    assert "Compute Flexible Committed Use Discounts - 3 Year" in query
    assert "Compute Flexible Committed Use Discounts - 1 Year" in query
    assert "wei_zheng" in query
    assert "MAX(export_time) AS source_export_time" in query
    assert "LIMIT 10" in query


def test_build_gcp_billing_query_without_limit() -> None:
    query = build_gcp_billing_query(billing_table="project.dataset.table")

    assert "LIMIT" not in query.splitlines()[-1]


def test_build_gcp_billing_summary_query_uses_partition_pruning() -> None:
    query = build_gcp_billing_summary_query(billing_table="project.dataset.table", limit=20)

    assert "`project.dataset.table`" in query
    assert "_PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end" in query
    assert "DATE(usage_start_time) >= @earliest_usage_date" in query
    assert "k8s-label/author" in query
    assert "k8s-label/repo" in query
    _assert_target_branch_label_keys(query)
    assert "target_branch" in query
    assert "resource_name" not in query
    assert "service.description AS service_name" in query
    assert "sku.description AS sku_name" in query
    assert "Cloud Logging" in query
    assert "Compute Flexible Committed Use Discounts - 3 Year" in query
    assert "Compute Flexible Committed Use Discounts - 1 Year" in query
    assert "wei_zheng" in query
    assert "LIMIT 20" in query


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


def test_fetch_gcp_billing_rows_uses_bigquery_client(monkeypatch) -> None:
    calls = {}

    class FakeScalarQueryParameter:
        def __init__(self, name, parameter_type, value):
            self.name = name
            self.parameter_type = parameter_type
            self.value = value

    class FakeQueryJobConfig:
        def __init__(self, query_parameters):
            self.query_parameters = query_parameters

    class FakeRow:
        def __init__(self, values):
            self.values = values

        def items(self):
            return self.values.items()

    class FakeQueryResult:
        def result(self, page_size):
            calls["page_size"] = page_size
            return [FakeRow({"account_id": "pingcap-testing-account"})]

    class FakeClient:
        def query(self, query, job_config):
            calls["query"] = query
            calls["job_config"] = job_config
            return FakeQueryResult()

    fake_bigquery = types.SimpleNamespace(
        Client=FakeClient,
        QueryJobConfig=FakeQueryJobConfig,
        ScalarQueryParameter=FakeScalarQueryParameter,
    )
    google_module = types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    cloud_module.bigquery = fake_bigquery
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)

    rows = list(
        fetch_gcp_billing_rows(
            billing_table="project.dataset.table",
            account_id="pingcap-testing-account",
            start_date=date(2026, 5, 17),
            end_date=date(2026, 5, 18),
            page_size=123,
            limit=10,
        )
    )

    assert rows == [{"account_id": "pingcap-testing-account"}]
    assert calls["page_size"] == 123
    assert "`project.dataset.table`" in calls["query"]
    params = {param.name: param.value for param in calls["job_config"].query_parameters}
    assert params == {
        "account_id": "pingcap-testing-account",
        "start_date": "2026-05-17",
        "end_date": "2026-05-18",
    }
