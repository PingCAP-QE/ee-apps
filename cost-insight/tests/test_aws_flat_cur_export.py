from datetime import date
from types import SimpleNamespace

import google.cloud

from cost_insight.sources.aws_flat_cur_export import (
    build_aws_flat_cur_summary_query,
    fetch_aws_flat_cur_summary_rows,
)


def test_build_aws_flat_cur_summary_query_scopes_external_partitions_and_costs() -> None:
    query = build_aws_flat_cur_summary_query(
        billing_table="gcp-digital-bi.aws_prod_billing.cost_insight_380838443567_prod_us_west_2_f04",
        export_partition_start=date(2026, 9, 1),
        export_partition_end=date(2026, 9, 1),
        usage_end_date=date(2026, 9, 5),
        limit=20,
    )

    assert "line_item_usage_account_id = @account_id" in query
    assert "(year = " not in query
    assert "DATE(bill_billing_period_start_date) BETWEEN @export_partition_start" in query
    assert "DATE(line_item_usage_start_date) <= @usage_end_date" in query
    assert "line_item_currency_code = 'USD'" in query
    assert "resource_tags_user_usedby = @usedby" in query
    assert "line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')" in query
    assert "NULLIF(TRIM(resource_tags_user_usedby), '') AS author" in query
    assert "NULLIF(TRIM(resource_tags_user_tenant), '') AS org" in query
    assert "NULLIF(TRIM(resource_tags_user_project), '') AS repo" in query
    assert "CAST(COALESCE(line_item_unblended_cost, 0) AS BIGNUMERIC) AS list_cost" in query
    assert "CAST(0 AS BIGNUMERIC) AS credit_amount" in query
    assert "LIMIT 20" in query


def test_fetch_aws_flat_cur_summary_rows_binds_usedby_parameter(monkeypatch) -> None:
    captured = {}

    class Client:
        def query(self, query, job_config):
            captured["query"] = query
            captured["parameters"] = job_config.query_parameters
            return SimpleNamespace(result=lambda *, page_size: ())

    monkeypatch.setattr(
        google.cloud,
        "bigquery",
        SimpleNamespace(
            Client=Client,
            QueryJobConfig=lambda *, query_parameters: SimpleNamespace(
                query_parameters=query_parameters
            ),
            ScalarQueryParameter=lambda name, type_, value: SimpleNamespace(
                name=name, type_=type_, value=value
            ),
        ),
        raising=False,
    )

    assert list(
        fetch_aws_flat_cur_summary_rows(
            billing_table="project.dataset.billing",
            account_id="380838443567",
            export_partition_start=date(2026, 9, 1),
            export_partition_end=date(2026, 9, 1),
            earliest_usage_date=date(2026, 9, 1),
            page_size=100,
        )
    ) == []
    assert "resource_tags_user_usedby = @usedby" in captured["query"]
    assert [(parameter.name, parameter.type_, parameter.value) for parameter in captured["parameters"]] == [
        ("account_id", "STRING", "380838443567"),
        ("usedby", "STRING", "prod-us-west-2-f04"),
        ("export_partition_start", "DATE", "2026-09-01"),
        ("export_partition_end", "DATE", "2026-09-01"),
        ("earliest_usage_date", "DATE", "2026-09-01"),
    ]
