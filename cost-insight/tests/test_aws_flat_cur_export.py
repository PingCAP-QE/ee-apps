from datetime import date

from cost_insight.sources.aws_flat_cur_export import build_aws_flat_cur_summary_query


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
    assert "resource_tags_user_usedby = 'prod-us-west-2-f04'" in query
    assert "line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')" in query
    assert "NULLIF(TRIM(resource_tags_user_usedby), '') AS author" in query
    assert "NULLIF(TRIM(resource_tags_user_tenant), '') AS org" in query
    assert "NULLIF(TRIM(resource_tags_user_project), '') AS repo" in query
    assert "CAST(COALESCE(line_item_unblended_cost, 0) AS BIGNUMERIC) AS list_cost" in query
    assert "CAST(0 AS BIGNUMERIC) AS credit_amount" in query
    assert "LIMIT 20" in query
