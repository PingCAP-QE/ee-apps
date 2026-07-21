from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import google.cloud

from cost_insight.sources.aws_billing_export import (
    build_aws_billing_summary_query,
    build_aws_unmatched_resource_query,
    fetch_aws_billing_summary_rows,
    fetch_aws_unmatched_resource_rows,
)


class _FakeScalarQueryParameter:
    def __init__(self, name, type_, value):
        self.name = name
        self.type_ = type_
        self.value = value


class _FakeQueryJobConfig:
    def __init__(self, query_parameters):
        self.query_parameters = query_parameters


class _FakeQueryResult:
    def __init__(self, rows, client):
        self._rows = rows
        self._client = client

    def result(self, *, page_size):
        self._client.page_size = page_size
        return self._rows


class _FakeBigQueryClient:
    def __init__(self, rows):
        self._rows = rows
        self.query_text = None
        self.job_config = None
        self.page_size = None

    def query(self, query, job_config):
        self.query_text = query
        self.job_config = job_config
        return _FakeQueryResult(self._rows, self)


def test_build_aws_billing_summary_query_contains_expected_filters() -> None:
    query = build_aws_billing_summary_query(
        billing_table="gcp-digital-bi.stg_cloud_billing.stg_aws_billing",
        limit=20,
    )

    assert "line_item_usage_account_id = @account_id" in query
    assert "PARSE_DATE('%Y%m%d', billing_month) BETWEEN @export_partition_start AND @export_partition_end" in query
    assert "WHERE kv.key = 'user_shared_pool'" in query
    assert "NULLIF(tag_cluster, '') AS `cluster`" in query
    assert "NULLIF(line_item_usage_type, '') AS usage_type" in query
    assert "MIN(usage_type) AS usage_type" in query
    assert "TO_JSON_STRING(STRUCT(`cluster` AS cluster, shared_pool AS shared_pool))" in query
    assert "END AS vendor_tags_json" in query
    assert "ROUND(SUM(net_cost - effective_cost), 2) AS credit_amount" in query
    assert "LIMIT 20" in query


def test_build_aws_unmatched_resource_query_contains_usage_seconds_logic() -> None:
    query = build_aws_unmatched_resource_query(
        billing_table="gcp-digital-bi.stg_cloud_billing.stg_aws_billing",
        limit=10,
    )

    assert "WHEN COUNTIF(pricing_unit = 'hour') = COUNT(*)" in query
    assert "resource_name IS NOT NULL" in query
    assert "WHERE kv.key = 'user_shared_pool'" in query
    assert "NULLIF(tag_cluster, '') AS `cluster`" in query
    assert "END AS vendor_tags_json" in query
    assert "ROUND(SUM(net_cost), 2) AS net_cost" in query
    assert "LIMIT 10" in query


def test_fetch_aws_billing_rows_use_bigquery_client(monkeypatch) -> None:
    summary_client = _FakeBigQueryClient(
        [{"account_id": "946646677266", "usage_date": "2026-05-01"}]
    )
    unmatched_client = _FakeBigQueryClient(
        [{"account_id": "946646677266", "resource_name": "i-123"}]
    )
    clients = [summary_client, unmatched_client]

    fake_bigquery = SimpleNamespace(
        Client=lambda: clients.pop(0),
        QueryJobConfig=_FakeQueryJobConfig,
        ScalarQueryParameter=_FakeScalarQueryParameter,
    )
    monkeypatch.setattr(google.cloud, "bigquery", fake_bigquery, raising=False)

    summary_rows = list(
        fetch_aws_billing_summary_rows(
            billing_table="billing.table",
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            earliest_usage_date=date(2026, 1, 1),
            page_size=50,
        )
    )
    unmatched_rows = list(
        fetch_aws_unmatched_resource_rows(
            billing_table="billing.table",
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            usage_start_date=date(2026, 5, 1),
            usage_end_date=date(2026, 5, 2),
            page_size=25,
        )
    )

    assert summary_rows == [{"account_id": "946646677266", "usage_date": "2026-05-01"}]
    assert unmatched_rows == [{"account_id": "946646677266", "resource_name": "i-123"}]
    assert "line_item_usage_account_id = @account_id" in summary_client.query_text
    assert summary_client.page_size == 50
    assert [param.name for param in summary_client.job_config.query_parameters] == [
        "account_id",
        "export_partition_start",
        "export_partition_end",
        "earliest_usage_date",
    ]
    assert "DATE(line_item_usage_start_date) BETWEEN @usage_start_date AND @usage_end_date" in unmatched_client.query_text
    assert unmatched_client.page_size == 25
    assert [param.name for param in unmatched_client.job_config.query_parameters] == [
        "account_id",
        "export_partition_start",
        "export_partition_end",
        "usage_start_date",
        "usage_end_date",
    ]
