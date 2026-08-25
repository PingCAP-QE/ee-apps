from datetime import date

import pytest

from cost_insight.sources import aws_split_cost_export
from cost_insight.sources.aws_split_cost_export import (
    _quote_bigquery_table,
    build_aws_split_cost_guardrail_query,
    build_aws_split_cost_parent_residual_allocation_query,
    build_aws_split_cost_summary_query,
    build_aws_split_cost_unmatched_resource_query,
)


def test_split_summary_query_conserves_parent_cost_at_parent_day_grain() -> None:
    query = build_aws_split_cost_summary_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    )

    assert "DATE(bill_billing_period_start_date)" in query
    assert "parent_keys" in query
    assert "raw.usage_date" in query
    assert "SUM(raw.direct_list_cost) AS direct_list_cost" in query
    assert "parent.direct_list_cost - COALESCE(SUM(child.split_list_cost), 0)" in query
    assert "line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')" in query
    assert "SavingsPlanNegation" not in query
    assert "COALESCE(line_item_unblended_cost, 0)" in query
    assert "COALESCE(split_line_item_split_cost, 0)" in query
    assert "'eks_parent_residual' AS source_allocation_scope" in query
    assert "eks_parent_tags AS" in query
    assert "REGEXP_CONTAINS(LOWER(COALESCE(child.resource_name, '')), r'(^|:)pod/')" in query
    assert "OR child.namespace IS NOT NULL" in query
    assert "THEN 'eks_unallocated'" in query
    assert "resource_tags_user_icost_owner_email" in query
    assert "COALESCE(split_line_item_split_usage, 0) AS split_usage_amount" in query
    assert "source_allocation_scope" in query
    assert "ROUND(SUM" not in query


def test_split_resource_query_keeps_parent_and_pod_identity() -> None:
    query = build_aws_split_cost_unmatched_resource_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    )

    assert "parent_resource_name" in query
    assert "split_usage_amount" in query
    assert "SUM(usage_amount) * 3600" in query
    assert "DATE(line_item_usage_start_date) <= @usage_end_date" in query
    assert "COALESCE(\n      NULLIF(line_item_resource_id, ''),\n      NULLIF(line_item_line_item_description, '')\n    ) AS resource_name" in query
    assert "AND resource_name IS NOT NULL" in query


def test_split_guardrail_uses_ce_list_cost_before_import() -> None:
    query = build_aws_split_cost_guardrail_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    )

    assert "child_split_list_cost - COALESCE(parent.parent_direct_list_cost, 0) > 0.01" in query
    assert "child_split_effective_cost - COALESCE(parent.parent_direct_effective_cost, 0) > 0.01" in query
    assert "line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')" in query
    assert "SavingsPlanNegation" not in query


def test_split_guardrail_can_bound_usage_date() -> None:
    query = build_aws_split_cost_guardrail_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost",
        include_usage_end_date=True,
    )

    assert "DATE(line_item_usage_start_date) <= @usage_end_date" in query


def test_split_resource_fetch_bounds_guardrail_to_requested_usage_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_fetch_rows(**kwargs):
        captured["guardrail_query"] = kwargs["guardrail_query"]
        return iter(())

    monkeypatch.setattr(aws_split_cost_export, "_fetch_rows", fake_fetch_rows)

    assert list(
        aws_split_cost_export.fetch_aws_split_cost_unmatched_resource_rows(
            billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost",
            account_id="946646677266",
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 1),
            usage_start_date=date(2026, 8, 1),
            usage_end_date=date(2026, 8, 15),
            page_size=100,
        )
    ) == []
    assert "DATE(line_item_usage_start_date) <= @usage_end_date" in captured["guardrail_query"]


def test_split_fetchers_can_skip_guardrail_after_a_cutover_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardrail_queries: list[str | None] = []

    def fake_fetch_rows(**kwargs):
        guardrail_queries.append(kwargs["guardrail_query"])
        return iter(())

    monkeypatch.setattr(aws_split_cost_export, "_fetch_rows", fake_fetch_rows)
    common = {
        "billing_table": "pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost",
        "account_id": "946646677266",
        "export_partition_start": date(2026, 8, 1),
        "export_partition_end": date(2026, 8, 1),
        "page_size": 100,
        "validate_guardrail": False,
    }

    assert list(
        aws_split_cost_export.fetch_aws_split_cost_summary_rows(
            **common,
            earliest_usage_date=date(2026, 8, 2),
            usage_end_date=date(2026, 8, 15),
        )
    ) == []
    assert list(
        aws_split_cost_export.fetch_aws_split_cost_unmatched_resource_rows(
            **common,
            usage_start_date=date(2026, 8, 2),
            usage_end_date=date(2026, 8, 15),
        )
    ) == []
    assert list(
        aws_split_cost_export.fetch_aws_split_cost_parent_residual_allocation_rows(
            **common,
            earliest_usage_date=date(2026, 8, 2),
            usage_end_date=date(2026, 8, 15),
        )
    ) == []
    assert guardrail_queries == [None, None, None]


def test_split_residual_ledger_query_keeps_parent_pod_grain() -> None:
    query = build_aws_split_cost_parent_residual_allocation_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    )

    assert "parent_resource_id" in query
    assert "pod_resource_id" in query
    assert "source_pod_split_list_cost" in query
    assert "parent_residual_list_cost" in query
    assert "'eks-tag:'" in query
    assert "REGEXP_CONTAINS(LOWER(COALESCE(resource_name, '')), r'(^|:)pod/')" in query
    assert "OR namespace IS NOT NULL" in query


@pytest.mark.parametrize(
    "table",
    (
        "project.dataset.table",
        "project-with-dash.dataset.table_202608",
    ),
)
def test_quote_bigquery_table_accepts_project_dataset_table(table: str) -> None:
    assert _quote_bigquery_table(table) == f"`{table}`"


@pytest.mark.parametrize(
    "table",
    (
        "project.dataset.table; DROP TABLE x",
        "project.dataset",
        "project.dataset.table`",
    ),
)
def test_quote_bigquery_table_rejects_untrusted_identifiers(table: str) -> None:
    with pytest.raises(ValueError, match="Invalid BigQuery table identifier"):
        _quote_bigquery_table(table)
