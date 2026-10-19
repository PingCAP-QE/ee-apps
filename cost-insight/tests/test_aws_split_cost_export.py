import re
import sqlite3
from contextlib import closing
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
    assert "ROUND(SUM(list_cost), 9) AS list_cost" in query
    assert "ROUND(SUM(effective_cost), 9) AS effective_cost" in query
    assert "'usedby', usedby" in query
    assert "usedby," in query.rsplit("GROUP BY", maxsplit=1)[1]


def test_split_parent_identity_selection_is_deterministic() -> None:
    query = build_aws_split_cost_summary_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    )
    parent_direct = query.split("parent_direct AS (", maxsplit=1)[1].split(
        "child_split AS (", maxsplit=1
    )[0]

    assert "ANY_VALUE" not in parent_direct
    for column in (
        "billing_account_id",
        "export_partition_date",
        "service_name",
        "sku_name",
        "usage_type",
        "region",
        "owner",
        "service",
        "project",
        "service_exec_id",
        "author_fallback",
        "org",
        "cluster",
        "shared_pool",
        "pricing_unit",
    ):
        assert f"MIN(raw.{column}) AS {column}" in parent_direct


def test_split_child_inherits_each_missing_parent_routing_tag() -> None:
    query = build_aws_split_cost_summary_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    )

    assert "COALESCE(child.author_fallback, parent.author_fallback) AS usedby" in query
    assert "COALESCE(child.cluster, parent.cluster) AS cluster" in query
    assert "COALESCE(child.shared_pool, parent.shared_pool) AS shared_pool" in query


def _execute_split_branches(query: str, rows: list[dict]) -> list[dict]:
    # Execute the generated normalization branches, before BQ-only final JSON/usage aggregation.
    ctes = "parent_keys AS (" + query.split("parent_keys AS (", maxsplit=1)[1].split(
        "\nSELECT\n  'aws' AS vendor,", maxsplit=1
    )[0]
    ctes = ctes.replace("r'(^|:)pod/'", "'(^|:)pod/'")
    columns = sorted(set(re.findall(r"\braw\.(\w+)", ctes)))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("STARTS_WITH", 2, lambda value, prefix: value.startswith(prefix))
        connection.create_function("REGEXP_CONTAINS", 2, lambda value, pattern: bool(re.search(pattern, value)))
        connection.create_function("GREATEST", 2, max)
        connection.execute(f"CREATE TABLE raw ({', '.join(columns)})")
        connection.executemany(
            f"INSERT INTO raw VALUES ({', '.join('?' for _ in columns)})",
            [[row.get(column) for column in columns] for row in rows],
        )
        return [dict(row) for row in connection.execute(f"WITH {ctes} SELECT * FROM branch_rows")]


@pytest.mark.parametrize(
    "query_builder",
    (build_aws_split_cost_summary_query, build_aws_split_cost_unmatched_resource_query),
    ids=("summary", "resource"),
)
@pytest.mark.parametrize(
    ("parent_orgs", "child_org", "expected_org"),
    (
        pytest.param(("tenant-a",), None, "tenant-a", id="inherit-missing"),
        pytest.param(("tenant-a", "tenant-a"), None, "tenant-a", id="same-parent-tenant"),
        pytest.param((None, "tenant-a"), None, "tenant-a", id="one-non-null-parent-tenant"),
        pytest.param((None,), None, None, id="no-parent-tenant"),
        pytest.param(("tenant-a", "tenant-b"), None, None, id="conflicting-parent-tenants"),
        pytest.param(("tenant-a",), "tenant-b", "tenant-b", id="explicit-child-wins"),
        pytest.param((None,), "tenant-b", "tenant-b", id="child-without-parent-tenant"),
        pytest.param(("tenant-a", "tenant-b"), "tenant-c", "tenant-c", id="child-with-conflict"),
    ),
)
def test_split_child_tenant_lineage_preserves_costs_and_business_labels(
    query_builder, parent_orgs, child_org, expected_org
) -> None:
    common = {
        "account_id": "946646677266",
        "usage_date": "2026-10-01",
        "source_export_time": "2026-10-02",
        "service_name": "AmazonEC2",
        "direct_list_cost": 0,
        "direct_effective_cost": 0,
        "split_list_cost": 0,
        "split_effective_cost": 0,
    }
    rows = [
        {
            **common,
            "resource_id": "i-parent",
            "resource_name": "i-parent",
            "org": org,
            "owner": "parent@example.com",
            "service": "parent-service",
            "project": "parent-project",
            "service_exec_id": "parent-exec",
            "author_fallback": "dbaas-control-plane-shoot",
            "direct_list_cost": 100,
            "direct_effective_cost": 80,
        }
        for org in parent_orgs
    ]
    rows.extend(
        (
            {
                **common,
                "resource_id": "arn:aws:eks:pod/child",
                "resource_name": "arn:aws:eks:pod/child",
                "parent_resource_name": "i-parent",
                "namespace": "tidb-test",
                "org": child_org,
                "split_list_cost": 30,
                "split_effective_cost": 20,
            },
            {
                **common,
                "resource_id": "i-unrelated",
                "resource_name": "i-unrelated",
                "org": "unrelated-tenant",
                "direct_list_cost": 7,
                "direct_effective_cost": 5,
            },
        )
    )
    query = query_builder(billing_table="project.dataset.split_cost")
    result = _execute_split_branches(query, rows)
    by_scope = {row["source_allocation_scope"]: row for row in result}
    assert len(result) == 3
    child = by_scope["eks_pod"]
    assert child["org"] == expected_org
    assert child["list_cost"] == 30
    assert child["effective_cost"] == child["net_cost"] == 20
    # Only tenant lineage is repaired; parent business labels and author do not leak.
    assert all(child[column] is None for column in (
        "owner", "service", "project", "service_exec_id", "author"
    ))
    assert child["usedby"] == "dbaas-control-plane-shoot"
    assert by_scope["direct"]["org"] == "unrelated-tenant"
    parent_org = min((org for org in parent_orgs if org is not None), default=None)
    assert by_scope["eks_parent_residual"]["org"] == parent_org
    assert sum(row["list_cost"] for row in result) == 100 * len(parent_orgs) + 7
    assert sum(row["effective_cost"] for row in result) == 80 * len(parent_orgs) + 5
    assert sum(row["net_cost"] for row in result) == 80 * len(parent_orgs) + 5


def test_split_resource_query_keeps_parent_and_pod_identity() -> None:
    query = build_aws_split_cost_unmatched_resource_query(
        billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    )

    assert "parent_resource_name" in query
    assert "split_usage_amount" in query
    assert "SUM(usage_amount) * 3600" in query
    assert "DATE(line_item_usage_start_date) <= @usage_end_date" in query
    assert "NULLIF(line_item_resource_id, '') AS resource_id" in query
    assert "NULLIF(TRIM(resource_tags_user_name), '')" in query
    assert "JSON_STRIP_NULLS(JSON_OBJECT(" in query
    assert "resource_tags.key_value" not in query
    assert "summary_vendor_tags_json" in query
    assert "AND resource_name IS NOT NULL" in query
    assert "CAST(NULL AS STRING) AS summary_resource_name" in query


def test_split_cost_tags_include_downstream_routing_labels() -> None:
    labels = {label for label, _ in aws_split_cost_export._AWS_SPLIT_COST_TAG_COLUMNS}

    assert {"cluster", "shared_pool", "tenant", "usedby"} <= labels


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
