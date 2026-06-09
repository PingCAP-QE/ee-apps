import sys
from types import ModuleType

import pytest

from cost_insight.common.bigquery import (
    BigQueryExecutionError,
    BigQueryParameter,
    execute_query,
)


def _install_fake_bigquery(monkeypatch, *, client_class) -> None:
    google_module = ModuleType("google")
    cloud_module = ModuleType("google.cloud")
    bigquery_module = ModuleType("google.cloud.bigquery")

    class FakeScalarQueryParameter:
        def __init__(self, name, type_, value):
            self.name = name
            self.type_ = type_
            self.value = value

    class FakeQueryJobConfig:
        def __init__(self, query_parameters):
            self.query_parameters = query_parameters

    bigquery_module.Client = client_class
    bigquery_module.QueryJobConfig = FakeQueryJobConfig
    bigquery_module.ScalarQueryParameter = FakeScalarQueryParameter
    cloud_module.bigquery = bigquery_module
    google_module.cloud = cloud_module

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery_module)


def test_execute_query_returns_rows_and_bytes_processed(monkeypatch) -> None:
    captured = {}

    class FakeJob:
        total_bytes_processed = 321

        def result(self):
            return [{"answer": 42}]

    class FakeClient:
        def query(self, query, job_config):
            captured["query"] = query
            captured["query_parameters"] = job_config.query_parameters
            return FakeJob()

    _install_fake_bigquery(monkeypatch, client_class=FakeClient)

    result = execute_query(
        "SELECT 42",
        parameters=(BigQueryParameter("limit", "INT64", 1),),
    )

    assert result.rows == ({"answer": 42},)
    assert result.total_bytes_processed == 321
    assert captured["query"] == "SELECT 42"
    assert captured["query_parameters"][0].name == "limit"
    assert captured["query_parameters"][0].type_ == "INT64"
    assert captured["query_parameters"][0].value == 1


def test_execute_query_wraps_client_errors_with_context(monkeypatch) -> None:
    class FakeClient:
        def query(self, query, job_config):
            raise ValueError("boom")

    _install_fake_bigquery(monkeypatch, client_class=FakeClient)

    with pytest.raises(BigQueryExecutionError, match="BigQuery query failed \\(ValueError\\).*boom"):
        execute_query(
            "SELECT broken",
            parameters=(BigQueryParameter("run_date", "DATE", "2026-06-08"),),
        )
