from datetime import date
from decimal import Decimal

import json

import pytest
from sqlalchemy import create_engine, text

from cost_insight.jobs.cli import build_parser
from cost_insight.jobs.validate_aws_reconciliation import (
    AWS_7266_ACCOUNT_ID,
    AWS_8728_ACCOUNT_ID,
    ReconciliationSource,
    build_bq_reconciliation_query,
    default_reconciliation_source,
    fetch_cost_explorer_amount,
    resolve_reconciliation_source,
    run_aws_reconciliation,
)


def test_cli_exposes_a_read_only_reconciliation_command() -> None:
    args = build_parser().parse_args(
        [
            "validate-aws-reconciliation",
            "--start-date", "2026-08-10",
            "--end-date", "2026-08-11",
            "--account-id", AWS_8728_ACCOUNT_ID,
            "--tenant", "1372813089209272198",
        ]
    )

    assert args.command == "validate-aws-reconciliation"
    assert args.tenant_tag_key == "tenant"


def test_source_profile_is_read_from_cost_sources_without_mutating_it() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cost_sources (
                  vendor TEXT, account_id TEXT, billing_account_id TEXT,
                  display_name TEXT, is_active INTEGER, source_table TEXT,
                  source_schema_version TEXT, source_available_from DATE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_sources VALUES
                ('aws', :account, NULL, 'AWS', 1, 'project.dataset.split',
                 'aws_split_cost_v1', '2026-08-02')
                """
            ),
            {"account": AWS_7266_ACCOUNT_ID},
        )

    source = resolve_reconciliation_source(engine, account_id=AWS_7266_ACCOUNT_ID)

    assert source == ReconciliationSource(
        AWS_7266_ACCOUNT_ID, "project.dataset.split", "aws_split_cost_v1"
    )


def test_default_source_selects_the_versioned_7266_split_export() -> None:
    source = default_reconciliation_source(AWS_7266_ACCOUNT_ID)

    assert source.schema_version == "aws_split_cost_v1"
    assert "946646677266_split_cost" in source.billing_table
    assert default_reconciliation_source(AWS_8728_ACCOUNT_ID).schema_version == "aws_cur_legacy_v1"


@pytest.mark.parametrize(
    ("source", "expected_tenant_column"),
    (
        (
            ReconciliationSource(AWS_8728_ACCOUNT_ID, "project.dataset.legacy"),
            "NULLIF(tag_tenant, '') AS org",
        ),
        (
            ReconciliationSource(
                AWS_7266_ACCOUNT_ID,
                "project.dataset.split",
                schema_version="aws_split_cost_v1",
            ),
            "NULLIF(TRIM(resource_tags_user_tenant), '') AS org",
        ),
    ),
)
def test_bq_query_is_tenant_scoped_and_uses_the_cost_adapter_contract(
    source: ReconciliationSource,
    expected_tenant_column: str,
) -> None:
    query = build_bq_reconciliation_query(source, metric="list_cost")

    assert "line_item_usage_account_id = @account_id" in query
    assert "usage_date >= @start_date" in query
    assert "usage_date < @end_date" in query
    assert "WHERE org = @tenant" in query
    assert expected_tenant_column in query
    assert "line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')" in query
    assert "WHERE parent_resource_name =" not in query


class _CostExplorer:
    def __init__(self) -> None:
        self.request = None

    def get_cost_and_usage(self, **kwargs):
        self.request = kwargs
        return {
            "ResultsByTime": [
                {"Total": {"UnblendedCost": {"Amount": "1.234"}}},
                {"Total": {"UnblendedCost": {"Amount": "-0.234"}}},
            ]
        }


def test_cost_explorer_request_uses_exclusive_end_and_matching_filters() -> None:
    client = _CostExplorer()

    amount = fetch_cost_explorer_amount(
        client,
        account_id=AWS_8728_ACCOUNT_ID,
        tenant="1372813089209272198",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 11),
    )

    assert amount == Decimal("1.000")
    assert client.request["TimePeriod"] == {"Start": "2026-08-10", "End": "2026-08-11"}
    assert client.request["Metrics"] == ["UnblendedCost"]
    assert client.request["Filter"]["And"] == [
        {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [AWS_8728_ACCOUNT_ID]}},
        {
            "Dimensions": {
                "Key": "RECORD_TYPE",
                "Values": ["Usage", "SavingsPlanCoveredUsage"],
            }
        },
        {"Tags": {"Key": "tenant", "Values": ["1372813089209272198"]}},
    ]


def _engine_with_reconciliation_facts():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for table in ("cost_bq_export_summary_daily", "cost_attribution_daily"):
            connection.execute(
                text(
                    f"""
                    CREATE TABLE {table} (
                      vendor TEXT, account_id TEXT, org TEXT, usage_date DATE,
                      list_cost NUMERIC, effective_cost NUMERIC, credit_amount NUMERIC, net_cost NUMERIC,
                      project TEXT, owner TEXT, service TEXT, service_exec_id TEXT,
                      allocate_method TEXT, attribution_source TEXT, attribution_status TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    f"""
                    INSERT INTO {table} VALUES
                    ('aws', :account, :tenant, '2026-08-10', 1.001, 1.001, 0, 1.001,
                     'p', 'o', 's', 'se', 'direct', 'summary', 'matched')
                    """
                ),
                {"account": AWS_8728_ACCOUNT_ID, "tenant": "tenant"},
            )
    return engine


class _Bq:
    def query(self, *_args, **_kwargs):
        raise AssertionError("fetch_bq_amount is replaced in this hermetic test")


class _Ce:
    pass


def test_reconciliation_allows_sub_cent_differences_and_is_read_only(monkeypatch) -> None:
    from cost_insight.jobs import validate_aws_reconciliation as reconciliation

    engine = _engine_with_reconciliation_facts()

    def raw_amount(*_args, **kwargs):
        return Decimal() if kwargs["metric"] == "credit_amount" else Decimal("1.004")

    monkeypatch.setattr(reconciliation, "fetch_bq_amount", raw_amount)
    monkeypatch.setattr(
        reconciliation, "fetch_cost_explorer_amount", lambda *_args, **_kwargs: Decimal("0.996")
    )

    result = run_aws_reconciliation(
        engine,
        bq_client=_Bq(),
        ce_client=_Ce(),
        source=ReconciliationSource(AWS_8728_ACCOUNT_ID, "project.dataset.table"),
        tenant="tenant",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 11),
    )

    assert result.passed
    assert result.metric == "list_cost"
    assert result.end_date == date(2026, 8, 11)
    assert result.attribution_breakdown[0]["project"] == "p"
    assert result.attribution_breakdown[0]["usage_date"] == "2026-08-10"
    assert result.attribution_breakdown[0]["amount"] == "1.001"
    json.dumps(result.as_dict())


def test_multi_day_reconciliation_rejects_failed_daily_records(monkeypatch) -> None:
    from cost_insight.jobs import validate_aws_reconciliation as reconciliation

    engine = _engine_with_reconciliation_facts()
    day1 = date(2026, 8, 10)
    day2 = date(2026, 8, 11)

    def amount_for_window(values, **kwargs):
        if kwargs["end_date"] - kwargs["start_date"] > (day2 - day1):
            return Decimal("4")
        return values[kwargs["start_date"]]

    def raw_amount(*_args, **kwargs):
        if kwargs["metric"] == "credit_amount":
            return Decimal()
        return amount_for_window({day1: Decimal("1"), day2: Decimal("3")}, **kwargs)

    def pipeline_amount(*_args, **kwargs):
        if kwargs["metric"] == "credit_amount":
            return Decimal()
        return amount_for_window({day1: Decimal("2"), day2: Decimal("2")}, **kwargs)

    monkeypatch.setattr(reconciliation, "fetch_bq_amount", raw_amount)
    monkeypatch.setattr(reconciliation, "fetch_tenant_amount", pipeline_amount)
    monkeypatch.setattr(
        reconciliation,
        "fetch_cost_explorer_amount",
        lambda *_args, **kwargs: amount_for_window(
            {day1: Decimal("2"), day2: Decimal("2")}, **kwargs
        ),
    )

    result = run_aws_reconciliation(
        engine,
        bq_client=_Bq(),
        ce_client=_Ce(),
        source=ReconciliationSource(AWS_8728_ACCOUNT_ID, "project.dataset.table"),
        tenant="tenant",
        start_date=day1,
        end_date=date(2026, 8, 12),
    )

    assert result.bq_raw == result.summary == result.attribution == result.cost_explorer == Decimal("4")
    assert not result.daily[0]["passed"]
    assert not result.daily[1]["passed"]
    assert len(result.daily[0]["pipeline_metrics"]) == 4
    assert not result.passed


def test_split_reconciliation_uses_ce_compatible_list_cost(monkeypatch) -> None:
    from cost_insight.jobs import validate_aws_reconciliation as reconciliation

    engine = _engine_with_reconciliation_facts()

    def raw_amount(*_args, **kwargs):
        return Decimal() if kwargs["metric"] == "credit_amount" else Decimal("1.001")

    monkeypatch.setattr(reconciliation, "fetch_bq_amount", raw_amount)
    monkeypatch.setattr(
        reconciliation, "fetch_cost_explorer_amount", lambda *_args, **_kwargs: Decimal("1.001")
    )

    result = run_aws_reconciliation(
        engine,
        bq_client=_Bq(),
        ce_client=_Ce(),
        source=ReconciliationSource(
            AWS_8728_ACCOUNT_ID, "project.dataset.table", schema_version="aws_split_cost_v1"
        ),
        tenant="tenant",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 11),
    )

    assert result.metric == "list_cost"
    assert result.passed


def test_reconciliation_rejects_an_empty_or_reversed_window() -> None:
    with pytest.raises(ValueError, match="start_date must be before end_date"):
        run_aws_reconciliation(
            object(), bq_client=None, ce_client=None,
            source=ReconciliationSource(AWS_8728_ACCOUNT_ID, "project.dataset.table"),
            tenant="tenant", start_date=date(2026, 8, 10), end_date=date(2026, 8, 10),
        )
