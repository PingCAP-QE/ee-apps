from datetime import date
from hashlib import sha256

import pytest

from cost_insight.sources.aws_billing_export import (
    build_aws_billing_summary_query,
    build_aws_unmatched_resource_query,
)
from cost_insight.sources.aws_flat_cur_export import build_aws_flat_cur_summary_query
from cost_insight.sources.aws_split_cost_export import (
    _AWS_SPLIT_COST_TAG_COLUMNS,
    build_aws_split_cost_guardrail_query,
    build_aws_split_cost_parent_residual_allocation_query,
    build_aws_split_cost_summary_query,
    build_aws_split_cost_unmatched_resource_query,
)
from cost_insight.sources.aws_warehouse_compat import build_warehouse_cur_relation

_TABLE = "project-with-dash.dataset.raw_cur"
_F04_TAG_COLUMNS = (
    "resource_tags_user_usedby",
    "resource_tags_user_tenant",
    "resource_tags_user_project",
    "resource_tags_user_cluster",
)
_SPLIT_TAG_COLUMNS = tuple(column for _, column in _AWS_SPLIT_COST_TAG_COLUMNS)
# SQL fingerprints captured before adding warehouse_raw, including optional bounds/limits.
_QUERY_CASES = (
    pytest.param(
        build_aws_billing_summary_query,
        {"usage_end_date": date(2026, 10, 12), "limit": 17},
        (),
        "a2bcc1c6bd9a953aec940f9ed360038557f228ec35795984c3cb95fdd6737003",
        id="legacy-summary",
    ),
    pytest.param(
        build_aws_unmatched_resource_query,
        {"limit": 17},
        (),
        "7b12095ab90fe5a428d40dbd94fadc1477adde5412b1b3a0252fc573b5db36ed",
        id="legacy-resource",
    ),
    pytest.param(
        build_aws_flat_cur_summary_query,
        {
            "export_partition_start": date(2026, 10, 1),
            "export_partition_end": date(2026, 10, 1),
            "usage_end_date": date(2026, 10, 12),
            "limit": 17,
        },
        _F04_TAG_COLUMNS,
        "0e44f9894eb4087b77b02957b82031a8396f069f6ac4219bec6d80d340488159",
        id="f04-summary",
    ),
    pytest.param(
        build_aws_split_cost_summary_query,
        {"include_usage_end_date": True, "limit": 17},
        _SPLIT_TAG_COLUMNS,
        "90d31c047db80ec19ddbb695dd099edf1a914b93272101079d47eef2ff2d9d67",
        id="split-summary",
    ),
    pytest.param(
        build_aws_split_cost_unmatched_resource_query,
        {"limit": 17},
        _SPLIT_TAG_COLUMNS,
        "ccd1a76ca7c442216f2a527e23fea0657bca9e89439d11cefd0894d69462c2d0",
        id="split-resource",
    ),
    pytest.param(
        build_aws_split_cost_guardrail_query,
        {"include_usage_end_date": True},
        _SPLIT_TAG_COLUMNS,
        "1b95c6313b12fb3cafa0d5840fe6f4cb7126d50b3075db08688bb2aa74d46642",
        id="split-guardrail",
    ),
    pytest.param(
        build_aws_split_cost_parent_residual_allocation_query,
        {"include_usage_end_date": True},
        _SPLIT_TAG_COLUMNS,
        "075c8ca90934479d4979e67a2582f065b1e22df7a596df8548e4888aee4ab0c7",
        id="split-residual",
    ),
)


def test_warehouse_relation_maps_billing_period_and_legacy_tags_without_normalizing() -> None:
    relation = build_warehouse_cur_relation(_TABLE)

    assert relation.startswith("(\n  SELECT\n    raw.*,")
    assert f"FROM `{_TABLE}` AS raw\n" in relation
    assert "raw.bill_billing_period_start_date >= TIMESTAMP(@export_partition_start)" in relation
    assert (
        "raw.bill_billing_period_start_date < "
        "TIMESTAMP(DATE_ADD(@export_partition_end, INTERVAL 1 DAY))"
    ) in relation
    assert "FORMAT_DATE('%Y%m%d', DATE(raw.bill_billing_period_start_date)) AS billing_month" in relation
    for column, key in (
        ("tag_used_by", "user_usedby"),
        ("tag_tenant", "user_tenant"),
        ("tag_project", "user_project"),
        ("tag_cluster", "user_cluster"),
    ):
        assert f"WHERE kv.key = '{key}'\n    LIMIT 1\n  ) AS {column}" in relation
    assert relation.count("SELECT kv.value\n    FROM UNNEST(raw.resource_tags.key_value)") == 4
    assert "user_used_by" not in relation
    assert "TRIM(" not in relation
    assert "NULLIF(" not in relation
    assert "EXCEPT" not in relation
    assert "REPLACE" not in relation


def test_warehouse_relation_maps_requested_resource_tags_and_deduplicates_aliases() -> None:
    relation = build_warehouse_cur_relation(
        _TABLE,
        tag_columns=(
            "resource_tags_user_usedby",
            "resource_tags_aws_eks_namespace",
            "resource_tags_user_usedby",
        ),
    )

    assert "WHERE kv.key = 'user_usedby'\n    LIMIT 1\n  ) AS resource_tags_user_usedby" in relation
    assert "WHERE kv.key = 'aws_eks_namespace'\n    LIMIT 1\n  ) AS resource_tags_aws_eks_namespace" in relation
    assert relation.count("AS resource_tags_user_usedby") == 1


@pytest.mark.parametrize("table", ("project.dataset.table", _TABLE))
def test_warehouse_relation_accepts_qualified_table_identifiers(table) -> None:
    assert f"FROM `{table}` AS raw" in build_warehouse_cur_relation(table)


@pytest.mark.parametrize(
    "column",
    (
        "tag_used_by",
        "resource_tags_",
        "resource_tags_user_usedby`",
        "resource_tags_user_usedby' OR TRUE --",
        "resource_tags_user_usedby; SELECT 1",
        "resource_tags_user_foo.bar",
        "resource_tags_user_foo-bar",
        "resource_tags_user_foo\nFROM other",
    ),
)
def test_warehouse_relation_rejects_untrusted_tag_columns(column) -> None:
    with pytest.raises(ValueError, match="Invalid CUR resource tag column"):
        build_warehouse_cur_relation(_TABLE, tag_columns=(column,))


@pytest.mark.parametrize("builder,kwargs,tag_columns,digest", _QUERY_CASES)
def test_default_builder_sql_is_unchanged(builder, kwargs, tag_columns, digest) -> None:
    query = builder(billing_table=_TABLE, **kwargs)

    assert sha256(query.encode()).hexdigest() == digest
    assert builder(billing_table=_TABLE, warehouse_raw=False, **kwargs) == query
    assert "FORMAT_DATE(" not in query


@pytest.mark.parametrize("builder,kwargs,tag_columns,digest", _QUERY_CASES)
def test_warehouse_builders_only_replace_the_source_relation(builder, kwargs, tag_columns, digest):
    original_query = builder(billing_table=_TABLE, **kwargs)
    relation = build_warehouse_cur_relation(_TABLE, tag_columns=tag_columns)
    query = builder(billing_table=_TABLE, warehouse_raw=True, **kwargs)

    # This also locks cost, attribution, JSON, identity, conservation and guardrail SQL.
    assert original_query.count(f"`{_TABLE}`") == 1
    assert query == original_query.replace(f"`{_TABLE}`", relation)


@pytest.mark.parametrize("builder,kwargs,tag_columns,digest", _QUERY_CASES)
@pytest.mark.parametrize(
    "table",
    (
        "project.dataset",
        "project.dataset.table; DROP TABLE x",
        "project.dataset.table`",
        "project.dataset.table*",
        "project.dataset.table\nUNION ALL SELECT 1",
        "project.dataset.table.extra",
    ),
)
def test_warehouse_builders_reject_untrusted_tables(builder, kwargs, tag_columns, digest, table):
    with pytest.raises(ValueError, match="Invalid BigQuery table identifier"):
        builder(billing_table=table, warehouse_raw=True, **kwargs)


def test_f04_warehouse_query_keeps_exact_tenant_and_currency_boundary() -> None:
    query = build_aws_flat_cur_summary_query(
        billing_table=_TABLE,
        export_partition_start=date(2026, 10, 1),
        export_partition_end=date(2026, 10, 1),
        usage_end_date=date(2026, 10, 12),
        warehouse_raw=True,
    )

    for clause in (
        "line_item_usage_account_id = @account_id",
        "line_item_currency_code = 'USD'",
        "resource_tags_user_usedby = @usedby",
        "line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')",
        "DATE(bill_billing_period_start_date) BETWEEN @export_partition_start AND @export_partition_end",
        "DATE(line_item_usage_start_date) >= @earliest_usage_date",
        "DATE(line_item_usage_start_date) <= @usage_end_date",
    ):
        assert clause in query
    for column in _F04_TAG_COLUMNS:
        assert f") AS {column}" in query
        assert f"NULLIF(TRIM({column}), '')" in query
    assert "(year =" not in query
    assert "(month =" not in query
    assert "resource_tags_user_icost_" not in query


@pytest.mark.parametrize(
    "builder",
    (
        build_aws_split_cost_summary_query,
        build_aws_split_cost_unmatched_resource_query,
        build_aws_split_cost_guardrail_query,
        build_aws_split_cost_parent_residual_allocation_query,
    ),
)
def test_split_warehouse_builders_keep_the_full_resource_tag_set(builder) -> None:
    query = builder(billing_table=_TABLE, warehouse_raw=True)

    for column in _SPLIT_TAG_COLUMNS:
        key = column.removeprefix("resource_tags_")
        assert f"WHERE kv.key = '{key}'\n    LIMIT 1\n  ) AS {column}" in query
    # raw.* retains original resource IDs, all split fields and diagnostic costs.
    assert "raw.*," in query
    assert "NULLIF(line_item_resource_id, '')" in query
    assert "NULLIF(split_line_item_parent_resource_id, '')" in query
