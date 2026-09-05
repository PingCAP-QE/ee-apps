from datetime import date
from decimal import Decimal

import pytest

from cost_insight.common.config import DEFAULT_AZURE_BILLING_TABLE, load_settings
from cost_insight.jobs import cli
from cost_insight.jobs.sync_azure_billing_summary import (
    _build_hash,
    _normalize_summary_row,
    _start_from_state,
)
from cost_insight.sources.azure_billing_export import build_azure_billing_summary_query


def test_azure_query_prunes_numeric_month_suffixes_and_uses_resource_location_first() -> None:
    query = build_azure_billing_summary_query(billing_table=DEFAULT_AZURE_BILLING_TABLE, limit=10)

    assert "REGEXP_CONTAINS(_TABLE_SUFFIX, r'^[0-9]{8}$')" in query
    assert "SAFE.PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) IS NOT NULL" in query
    assert "DATE_TRUNC(SAFE.PARSE_DATE('%Y%m%d', _TABLE_SUFFIX), MONTH)" in query
    assert "NULLIF(CAST(billingCurrency AS STRING), '') AS currency" in query
    assert query.index("CAST(location AS STRING)") < query.index("CAST(resourceLocation AS STRING)")
    assert "LIMIT 10" in query


def test_azure_cli_exposes_replacement_flags_and_rejects_windows_over_five_days(
    monkeypatch,
) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "sync-azure-billing-summary",
            "--account-id",
            "aaa5414d-7537-4e24-99bd-a7a841221810",
            "--replace-existing-partitions",
            "--replace-usage-start-date",
            "2026-04-01",
            "--replace-usage-end-date",
            "2026-04-03",
            "--export-partition-start",
            "2026-04-01",
            "--export-partition-end",
            "2026-04-01",
        ]
    )
    assert args.replace_usage_start_date == date(2026, 4, 1)
    assert args.replace_usage_end_date == date(2026, 4, 3)

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda require_database=True: type("Settings", (), {"log_level": "INFO"})(),
    )
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    with pytest.raises(ValueError, match="maximum five-day"):
        cli.main(
            [
                "sync-azure-billing-summary",
                "--export-partition-start",
                "2026-04-01",
                "--export-partition-end",
                "2026-04-06",
            ]
        )


def test_azure_query_rejects_non_wildcard_table_identifier() -> None:
    with pytest.raises(ValueError, match=r"project.dataset.table_\* identifier"):
        build_azure_billing_summary_query(billing_table="project.dataset.table")


def test_azure_normalization_preserves_decimal_values_and_hash_dimensions() -> None:
    row = _normalize_summary_row(
        {
            "account_id": "sub-1",
            "billing_account_id": "billing-1",
            "export_partition_date": date(2026, 4, 1),
            "usage_date": date(2026, 4, 2),
            "service_name": "Microsoft.Compute",
            "sku_name": "D2s",
            "usage_type": "Usage",
            "currency": "USD",
            "region": "eastus2",
            "resource_name": "/subscriptions/sub-1/resourceGroups/rg/providers/x/y",
            "list_cost": "1.234567891",
            "effective_cost": "1.000000001",
            "net_cost": "1.000000001",
        }
    )

    assert row["list_cost"] == Decimal("1.234567891")
    assert row["effective_cost"] == Decimal("1.000000001")
    assert row["credit_amount"] == 0
    changed = dict(row, currency="EUR")
    assert _build_hash(row) != _build_hash(changed)


def test_azure_state_advances_by_month_and_keeps_initial_lookback_month_aligned() -> None:
    settings = load_settings({}, require_database=False).azure_billing
    settings = settings.__class__(**{**settings.__dict__, "export_overlap_days": 0})

    assert _start_from_state(
        {"export_partition_end": "2026-04-01"}, date(2026, 5, 1), settings
    ) == date(2026, 5, 1)
    settings = settings.__class__(**{**settings.__dict__, "export_overlap_days": 1})
    assert _start_from_state(
        {"export_partition_end": "2026-04-01"}, date(2026, 5, 1), settings
    ) == date(2026, 3, 1)
    settings = settings.__class__(**{**settings.__dict__, "sync_initial_lookback_days": 40})
    assert _start_from_state({}, date(2026, 5, 20), settings) == date(2026, 4, 1)
