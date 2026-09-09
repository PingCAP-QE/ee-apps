from datetime import date
from types import SimpleNamespace

import pytest

from cost_insight.sources.alibaba_billing_export import (
    DEFAULT_ALIBABA_BILLING_TABLE,
    build_alibaba_billing_summary_query,
    fetch_alibaba_billing_summary_rows,
)


def test_alibaba_query_selects_owner_account_and_prunes_daily_shards() -> None:
    query = build_alibaba_billing_summary_query(
        billing_table=DEFAULT_ALIBABA_BILLING_TABLE,
        limit=10,
    )

    assert "CAST(owner_account_id AS STRING) = @account_id" in query
    assert "partition_date BETWEEN @export_partition_start AND @export_partition_end" in query
    assert "_TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', @export_partition_start)" in query
    assert "DATE(usage_start_time) AS usage_date" in query
    assert "MIN(usage_type) AS usage_type" in query
    assert "GROUP BY\n  vendor," in query
    assert "GROUP BY\n  vendor,\n  account_id,\n  billing_account_id,\n  export_partition_date,\n  usage_date,\n  service_name,\n  sku_name,\n  usage_type," not in query
    assert "pretax_gross_amount AS BIGNUMERIC" in query
    assert "amount_after_discount AS BIGNUMERIC" in query
    assert "-COALESCE(SAFE_CAST(deducted_by_coupons AS BIGNUMERIC), 0) AS credit_amount" in query
    assert "JSON_OBJECT('instance_tag', instance_tag, 'tenant'" in query
    assert "ROUND(SUM(list_cost), 9) AS list_cost" in query
    assert "LIMIT 10" in query


@pytest.mark.parametrize(
    "billing_table",
    ("project.dataset.table", "project.dataset.daily_*", "project.dataset.daily_en_20260801"),
)
def test_alibaba_query_rejects_untrusted_table_identifiers(billing_table: str) -> None:
    with pytest.raises(ValueError, match=r"project.dataset.daily_en_\* identifier"):
        build_alibaba_billing_summary_query(billing_table=billing_table)


def test_fetch_alibaba_rows_uses_parameterized_query(monkeypatch) -> None:
    captured = {}

    class Client:
        def query(self, query, *, job_config):
            captured["query"] = query
            captured["job_config"] = job_config
            return SimpleNamespace(result=lambda *, page_size: [{"account_id": "5028760335873601"}])

    fake_bigquery = SimpleNamespace(
        Client=lambda: Client(),
        QueryJobConfig=lambda *, query_parameters: SimpleNamespace(query_parameters=query_parameters),
        ScalarQueryParameter=lambda name, kind, value: (name, kind, value),
    )
    import google.cloud

    monkeypatch.setattr(google.cloud, "bigquery", fake_bigquery, raising=False)

    assert list(
        fetch_alibaba_billing_summary_rows(
            billing_table=DEFAULT_ALIBABA_BILLING_TABLE,
            account_id="5028760335873601",
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 31),
            earliest_usage_date=date(2026, 8, 1),
            page_size=500,
        )
    ) == [{"account_id": "5028760335873601"}]
    assert "@account_id" in captured["query"]
    assert captured["job_config"].query_parameters[0] == (
        "account_id",
        "STRING",
        "5028760335873601",
    )
