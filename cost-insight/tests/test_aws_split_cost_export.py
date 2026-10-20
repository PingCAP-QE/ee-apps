import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from cost_insight.jobs.materialize_resource_serving import build_resource_serving_rows
from cost_insight.jobs.sync_gcp_billing_summary import _normalize_summary_row
from cost_insight.jobs.sync_gcp_unmatched_resources import (
    _normalize_resource_row,
    replace_unmatched_resource_usage_dates,
    write_unmatched_resource_rows,
)
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


def _execute_split_branches(
    query: str, rows: list[dict], *, aggregate: bool = False
) -> list[dict]:
    # Start from normalized raw fixtures, but execute the real final projection when requested.
    sql = "parent_keys AS (" + query.split("parent_keys AS (", maxsplit=1)[1]
    if not aggregate:
        sql = sql.split("\nSELECT\n  'aws' AS vendor,", maxsplit=1)[0]
        sql += " SELECT * FROM branch_rows"
    sql = sql.replace("r'(^|:)pod/'", "'(^|:)pod/'")
    columns = sorted(set(re.findall(r"\braw\.(\w+)", sql)))

    class CountIf:
        def __init__(self):
            self.count = 0

        def step(self, value):
            self.count += bool(value)

        def finalize(self):
            return self.count

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("STARTS_WITH", 2, lambda value, prefix: value.startswith(prefix))
        connection.create_function("REGEXP_CONTAINS", 2, lambda value, pattern: bool(re.search(pattern, value)))
        connection.create_function("GREATEST", 2, max)
        connection.create_aggregate("COUNTIF", 1, CountIf)
        connection.create_function("TO_JSON_STRING", 1, lambda value: value)
        connection.create_function(
            "JSON_STRIP_NULLS", 1,
            lambda value: json.dumps({k: v for k, v in json.loads(value).items() if v is not None}),
        )
        connection.execute(f"CREATE TABLE raw ({', '.join(columns)})")
        connection.executemany(
            f"INSERT INTO raw VALUES ({', '.join('?' for _ in columns)})",
            [[row.get(column) for column in columns] for row in rows],
        )
        return [dict(row) for row in connection.execute(f"WITH {sql}")]


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


def _resource_label_snapshots(scope: str, amounts: tuple[str, str]) -> list[dict]:
    resource = {
        "direct": "i-direct", "eks_unallocated": "vol-tag-change",
        "eks_pod": "arn:aws:eks:pod/tag-change", "split_child": "arn:aws:ecs:task/tag-change",
    }[scope]
    common = {
        "account_id": "946646677266", "billing_account_id": "payer",
        "usage_date": "2026-09-01", "export_partition_date": "2026-09-01",
        "service_name": "AmazonEC2", "sku_name": "gp3-throughput", "region": "us-west-2",
        "resource_id": resource, "resource_name": resource,
        "org": "tenant-a", "cluster": "cluster-a", "shared_pool": "pool-a",
        "author_fallback": "dbaas-control-plane-shoot", "pricing_unit": "hour",
        "direct_list_cost": 0, "direct_effective_cost": 0,
        "split_list_cost": 0, "split_effective_cost": 0,
    }
    is_child = scope in ("eks_pod", "split_child")
    rows = []
    for index, amount in enumerate(amounts):
        tags = {"environment": "staging", "shared_pool": "pool-a"}
        if index == 0:
            tags["component"] = "pd"
        row = {**common, "vendor_tags_json": json.dumps(tags, sort_keys=True),
               "source_export_time": f"2026-09-01T{21 - 2 * index}:00:00Z"}
        if is_child:
            row.update(parent_resource_name="i-parent", org=None,
                       namespace="tidb-test" if scope == "eks_pod" else None,
                       split_list_cost=float(amount), split_effective_cost=(10 - index) / 1000,
                       split_usage_amount=index + 2)
        else:
            row.update(direct_list_cost=float(amount), direct_effective_cost=(10 - index) / 1000,
                       usage_amount=index + 2)
        rows.append(row)
    if is_child:
        rows.append({**common, "resource_id": "i-parent", "resource_name": "i-parent",
                     "direct_list_cost": 1, "direct_effective_cost": 0.8,
                     "usage_amount": 24, "vendor_tags_json": '{}',
                     "source_export_time": "2026-09-01T20:00:00Z"})
    return rows


def _resource_storage_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE cost_unmatched_resource_daily (
              id INTEGER PRIMARY KEY, vendor TEXT, account_id TEXT, billing_account_id TEXT,
              export_partition_date TEXT, usage_date TEXT, region TEXT, service_name TEXT,
              sku_name TEXT, namespace TEXT, author TEXT, org TEXT, repo TEXT, target_branch TEXT,
              vendor_tags_json TEXT, resource_name TEXT, resource_id TEXT, parent_resource_name TEXT,
              source_allocation_scope TEXT, workload_name TEXT, workload_type TEXT, owner TEXT,
              service TEXT, project TEXT, service_exec_id TEXT, usage_seconds REAL,
              list_cost REAL, effective_cost REAL, credit_amount REAL, net_cost REAL,
              source_export_time TEXT, source_row_hash TEXT, source_summary_row_hash TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(vendor, account_id, export_partition_date, source_row_hash)
            )
        """))
    return engine


@pytest.mark.parametrize("scope", ("direct", "eks_unallocated", "eks_pod", "split_child"))
@pytest.mark.parametrize("replacement", (False, True), ids=("upsert", "replace-day"))
@pytest.mark.parametrize("amounts", (("0.016287809", "0.015277778"), ("0.015277778", "0.015277778")))
def test_split_resource_label_changes_conserve_cost_through_storage_and_serving(
    scope: str, replacement: bool, amounts: tuple[str, str]
) -> None:
    raw = _resource_label_snapshots(scope, amounts)
    query = build_aws_split_cost_unmatched_resource_query(billing_table="project.dataset.table")
    resources = [_normalize_resource_row(row) for row in _execute_split_branches(query, raw, aggregate=True)]
    summary_query = build_aws_split_cost_summary_query(billing_table="project.dataset.table")
    summaries = [_normalize_summary_row(row) for row in _execute_split_branches(summary_query, raw, aggregate=True)]
    metrics = ("list_cost", "effective_cost", "credit_amount", "net_cost")
    expected = {metric: sum((row[metric] or Decimal() for row in summaries), Decimal()) for metric in metrics}
    engine = _resource_storage_engine()
    try:
        for _ in range(2):
            if replacement:
                replace_unmatched_resource_usage_dates(
                    engine, iter(resources), row_count=len(resources), vendor="aws",
                    account_id="946646677266", usage_start_date=date(2026, 9, 1),
                    usage_end_date=date(2026, 9, 1), dry_run=False, batch_size=1,
                )
            else:
                write_unmatched_resource_rows(engine, resources, dry_run=False)
            with engine.connect() as connection:
                stored = [dict(row) for row in connection.execute(text(
                    "SELECT * FROM cost_unmatched_resource_daily"
                )).mappings()]
            for metric in metrics:
                actual = sum((Decimal(str(row[metric] or 0)) for row in stored), Decimal())
                assert actual.quantize(Decimal("0.000000001")) == expected[metric]
            assert len(stored) == len(resources)

        assert len({row["source_row_hash"] for row in resources}) == len(resources)
        summary_hashes = {row["source_row_hash"] for row in summaries}
        assert {row["source_summary_row_hash"] for row in stored} <= summary_hashes
        detail = next(row for row in resources if row["source_allocation_scope"] == scope)
        assert detail["list_cost"] == sum(map(Decimal, amounts))
        assert detail["usage_seconds"] == Decimal(18000)
        assert detail["source_export_time"] == datetime(2026, 9, 1, 21, tzinfo=UTC).replace(tzinfo=None)
        assert json.loads(detail["vendor_tags_json"])["component"] == "pd"
        reverse = [_normalize_resource_row(row) for row in _execute_split_branches(query, raw[::-1], aggregate=True)]
        assert sorted((r["source_row_hash"], r["vendor_tags_json"]) for r in reverse) == sorted(
            (r["source_row_hash"], r["vendor_tags_json"]) for r in resources
        )
        sources = [{**row, "source_summary_row_hash": row["source_row_hash"],
                    "source_fact_hash": row["source_row_hash"]} for row in summaries]
        serving = build_resource_serving_rows(
            source_rows=sources, detail_rows=stored, basis_key="native",
            materialization_version="test", calculated_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        assert sum((row["fallback_list_cost"] for row in serving), Decimal()) == 0
        for metric in metrics:
            assert sum((row[metric] or Decimal() for row in serving), Decimal()) == expected[metric]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("units", "seconds"),
    ((("hour", "hour"), 18000), (("minute", "minute"), 300),
     (("second", "second"), 5), (("gb-month", "gb-month"), None),
     (("hour", "minute"), None), ((None, "hour"), None)),
)
def test_split_resource_label_rollup_only_combines_compatible_usage_units(units, seconds):
    raw = _resource_label_snapshots("direct", ("0.016287809", "0.015277778"))
    for row, unit in zip(raw, units):
        row["pricing_unit"] = unit
    query = build_aws_split_cost_unmatched_resource_query(billing_table="project.dataset.table")
    result = _execute_split_branches(query, raw, aggregate=True)
    assert len(result) == 1
    assert result[0]["usage_seconds"] == seconds


@pytest.mark.parametrize(
    ("field", "value"),
    (("org", "tenant-b"), ("shared_pool", "pool-b"), ("cluster", "cluster-b"),
     ("author_fallback", "other-author"), ("owner", "owner@example.com"),
     ("project", "other-project"), ("resource_name", "vol-other")),
)
def test_split_resource_aggregation_keeps_routing_and_business_identities_separate(field, value):
    raw = _resource_label_snapshots("eks_unallocated", ("0.016287809", "0.015277778"))
    raw[1][field] = value
    query = build_aws_split_cost_unmatched_resource_query(billing_table="project.dataset.table")
    resources = [_normalize_resource_row(row) for row in _execute_split_branches(query, raw, aggregate=True)]
    assert len(resources) == 2
    assert len({row["source_row_hash"] for row in resources}) == 2
    assert sum((row["list_cost"] for row in resources), Decimal()) == Decimal("0.031565587")


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
