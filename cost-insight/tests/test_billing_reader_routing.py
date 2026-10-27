from datetime import date
from importlib import import_module
from unittest.mock import Mock

import pytest
from google.cloud import bigquery

_COMMON = {
    "billing_table": "project.dataset.billing",
    "account_id": "account",
    "export_partition_start": date(2026, 10, 1),
    "export_partition_end": date(2026, 10, 1),
    "page_size": 2,
}
_SUMMARY = {"earliest_usage_date": date(2026, 10, 11)}
_RESOURCE = {"usage_start_date": date(2026, 10, 11), "usage_end_date": date(2026, 10, 12)}


@pytest.mark.parametrize(
    "module_name,fetch_name,extra",
    [
        ("aws_billing_export", "fetch_aws_billing_summary_rows", _SUMMARY),
        ("aws_billing_export", "fetch_aws_unmatched_resource_rows", _RESOURCE),
        ("aws_flat_cur_export", "fetch_aws_flat_cur_summary_rows", _SUMMARY),
        ("aws_split_cost_export", "fetch_aws_split_cost_summary_rows", _SUMMARY),
        ("aws_split_cost_export", "fetch_aws_split_cost_unmatched_resource_rows", _RESOURCE),
        (
            "aws_split_cost_export",
            "fetch_aws_split_cost_parent_residual_allocation_rows",
            {**_SUMMARY, "usage_end_date": date(2026, 10, 12)},
        ),
        ("gcp_billing_export", "fetch_gcp_billing_summary_rows", _SUMMARY),
        ("gcp_billing_export", "fetch_gcp_unmatched_resource_rows", _RESOURCE),
        (
            "alibaba_billing_export",
            "fetch_alibaba_billing_summary_rows",
            {**_SUMMARY, "billing_table": "project.dataset.daily_en_*"},
        ),
        (
            "azure_billing_export",
            "fetch_azure_billing_summary_rows",
            {**_SUMMARY, "billing_table": "project.dataset.azure_billing_cost_*"},
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_billing_fetchers_use_the_configured_client(monkeypatch, module_name, fetch_name, extra):
    module = import_module(f"cost_insight.sources.{module_name}")
    client = Mock()
    client.query.return_value.result.return_value = ()
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "build_billing_bigquery_client", factory)
    monkeypatch.setattr(
        bigquery,
        "Client",
        Mock(side_effect=AssertionError("billing reader bypassed the configured client")),
    )

    assert list(getattr(module, fetch_name)(**{**_COMMON, **extra})) == []
    factory.assert_called_once_with()
    assert client.query.called
