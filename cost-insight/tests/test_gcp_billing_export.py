import sys
import types
from datetime import date
from decimal import Decimal

from cost_insight.sources.gcp_billing_export import (
    build_gcp_billing_query,
    decimal_or_none,
    fetch_gcp_billing_rows,
)


def test_build_gcp_billing_query_keeps_expected_dimensions() -> None:
    query = build_gcp_billing_query(billing_table="project.dataset.table", limit=10)

    assert "`project.dataset.table`" in query
    assert "k8s-label/author" in query
    assert "k8s-label/repo" in query
    assert "k8s-workload-name" in query
    assert "cost_at_list" in query
    assert "pricing_unit NOT IN ('hour', 'minute', 'second')" in query
    assert "ROUND(SUM(amount_in_pricing_units) * 60, 2)" in query
    assert "MAX(export_time) AS source_export_time" in query
    assert "LIMIT 10" in query


def test_build_gcp_billing_query_without_limit() -> None:
    query = build_gcp_billing_query(billing_table="project.dataset.table")

    assert "LIMIT" not in query.splitlines()[-1]


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
