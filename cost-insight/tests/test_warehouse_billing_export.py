"""Warehouse reads use only fake BigQuery jobs and table metadata (no ADC/network)."""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cost_insight.sources import warehouse_billing_export as source

PROJECT = "warehouse-f80fbbe8e8"
TODAY = date(2026, 11, 4)
END = date(2026, 11, 2)
START = END - timedelta(days=4)
ACCOUNT = "account' OR TRUE --"


def metadata(**changes):
    return SimpleNamespace(table_type="TABLE", expires=None, time_partitioning=None,
                           modified=datetime(2026, 11, 4, tzinfo=timezone.utc),
                           num_rows=100, etag="stable", **changes)


def client_for(*, end=END, discovery=None, controls=None, rows=None):
    client = Mock()
    client.get_table.return_value = metadata()
    results = [discovery if discovery is not None else [{"usage_date": end}],
               controls if controls is not None else [{"usage_date": end, "n": 1}],
               rows if rows is not None else [{"usage_date": end, "raw": "unchanged"}]]
    jobs = []
    for i, result in enumerate(results):
        preview = Mock(total_bytes_processed=100, job_id=f"preview-{i}")
        actual = Mock(total_bytes_processed=90, job_id=f"actual-{i}")
        actual.result.return_value = result
        jobs.extend((preview, actual))
    client.query.side_effect = jobs
    return client, jobs


def fetch(client, vendor="aws", **changes):
    args = dict(vendor=vendor, account_id=ACCOUNT, project=PROJECT, today=TODAY, heartbeat=Mock())
    args.update(changes)
    return source.fetch_warehouse_window(client, **args)


def parameters(call):
    return {p.name: p.value for p in call.kwargs["job_config"].query_parameters}


@pytest.mark.parametrize("vendor", ["aws", "azure"])
def test_both_routes_resolve_latest_actual_day_and_apply_all_query_bounds(vendor, caplog):
    client, jobs = client_for()
    heartbeat = Mock()
    with caplog.at_level(logging.INFO, logger=source.__name__):
        window = fetch(client, vendor, heartbeat=heartbeat)
    assert window.start == START and window.end == END  # Not UTC yesterday, which has no rows.
    assert window.rows == [{"usage_date": END, "raw": "unchanged"}]
    assert window.controls == [{"usage_date": END, "n": 1}]
    assert len(client.query.call_args_list) == 6
    for index, call in enumerate(client.query.call_args_list):
        assert call.kwargs["timeout"] == 30
        assert call.kwargs["retry"] is None and call.kwargs["job_retry"] is None
        config = call.kwargs["job_config"]
        assert config.maximum_bytes_billed == 2 * 1024**3
        assert config.use_query_cache is False
        assert bool(config.dry_run) == (index % 2 == 0)
        assert ACCOUNT not in call.args[0]  # Identity is always a parameter, never SQL text.
        bound = parameters(call)
        assert bound["account_id"] == ACCOUNT
        expected_start, expected_end = ((TODAY - timedelta(days=14), TODAY - timedelta(days=1))
                                        if index < 2 else (START, END))
        assert bound["earliest_usage_date"] == expected_start
        assert bound["usage_end_date"] == expected_end
        if "export_partition_start" in bound:
            assert bound["export_partition_start"] == expected_start.replace(day=1)
            assert bound["export_partition_end"] == expected_end.replace(day=1)
    for job in jobs[1::2]:
        job.result.assert_called_once_with(page_size=1000, timeout=900, retry=None, job_retry=None)
        job.cancel.assert_not_called()
    for call in client.get_table.call_args_list:
        assert call.kwargs == {"timeout": 30, "retry": None}
    expected_tables = ({f"{PROJECT}.raw_aws_billing.cur_line_items"} if vendor == "aws" else {
        f"{PROJECT}.raw_azure_billing.azure_billing_cost_20261001",
        f"{PROJECT}.raw_azure_billing.azure_billing_cost_20261101"})
    assert set(window.snapshots) == expected_tables
    assert client.get_table.call_count == 7 * len(expected_tables)  # Initial, before/after each read.
    assert heartbeat.call_count >= 10
    events = [json.loads(record.message) for record in caplog.records]
    previews = [event for event in events if event["event"] == "query_preflight"]
    actuals = [event for event in events if event["event"] == "query_fetched"]
    assert len(previews) == len(actuals) == 3
    assert all(event["estimated_bytes"] == 100 and event["job_id"] for event in previews)
    assert all(event["bytes_processed"] == 90 and event["job_id"] for event in actuals)


def test_aws_uses_existing_warehouse_raw_builder_and_exact_usage_listcost_semantics():
    client, _ = client_for()
    fetch(client)
    control, summary = [client.query.call_args_list[i].args[0] for i in (2, 4)]
    assert "line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')" in control
    assert "THEN COALESCE(line_item_unblended_cost, 0) ELSE 0 END) AS list_cost" in control
    assert "AS effective_cost" not in control and "AS net_cost" not in control
    assert "line_item_currency_code IS NULL OR line_item_currency_code != 'USD'" in control
    assert "resource_tags.key_value" in summary and "AS billing_month" in summary
    assert "bill_billing_period_start_date >= TIMESTAMP(@export_partition_start)" in summary
    assert "line_item_currency_code = 'USD'" in summary
    assert "'USD' AS currency" in summary and "ROUND(SUM(effective_cost), 2)" in summary


def test_azure_uses_explicit_numeric_mca_months_never_wildcard_from():
    client, _ = client_for()
    fetch(client, "azure")
    for call in client.query.call_args_list:
        sql = call.args[0]
        assert "UNION ALL" in sql
        assert f"FROM `{PROJECT}.raw_azure_billing.azure_billing_cost_*`" not in sql
        assert "azure_billing_cost_20261001`" in sql
        assert "azure_billing_cost_20261101`" in sql
        assert "'20261001' AS _TABLE_SUFFIX" in sql
        assert "'20261101' AS _TABLE_SUFFIX" in sql
        assert "BETWEEN @earliest_usage_date AND @usage_end_date" in sql
        assert "CAST(SubscriptionId AS STRING) = @account_id" in sql
    control = client.query.call_args_list[2].args[0]
    for metric in ("list_cost", "effective_cost", "credit_amount", "net_cost"):
        assert f"AS {metric}" in control
    assert "AS invalid_money" in control
    assert "billingCurrency IS NULL OR billingCurrency != 'USD'" in control
    summary = client.query.call_args_list[4].args[0]
    assert "SAFE_CAST(paygCostInBillingCurrency AS NUMERIC) AS list_cost" in summary
    assert "NULLIF(CAST(tags AS STRING), '') AS vendor_tags_json" in summary


def test_same_month_has_no_union_and_freshness_boundary_is_accepted():
    today = date(2026, 10, 30)
    client, _ = client_for(end=today - timedelta(days=7))
    window = fetch(client, "azure", today=today)
    assert window.end == today - timedelta(days=7)
    assert len(window.snapshots) == 1
    assert all("UNION ALL" not in call.args[0] for call in client.query.call_args_list)


@pytest.mark.parametrize("end,match", [(TODAY, "outside"),
    (TODAY - timedelta(days=15), "outside"), (TODAY - timedelta(days=8), "stale")])
@pytest.mark.parametrize("vendor", ["aws", "azure"])
def test_future_unbounded_or_stale_discovery_refuses_further_reads(end, match, vendor):
    client, _ = client_for(end=end)
    with pytest.raises(ValueError, match=match):
        fetch(client, vendor)
    assert client.query.call_count == 2


@pytest.mark.parametrize("discovery,match", [([], "No actual usage"),
    ([{"usage_date": datetime(2026, 11, 2)}], "not a timestamp"),
    ([{"usage_date": None}], "Invalid isoformat")])
def test_empty_or_invalid_actual_day_is_not_yesterday_or_zero(discovery, match):
    client, _ = client_for(discovery=discovery)
    with pytest.raises(ValueError, match=match):
        fetch(client)
    assert client.query.call_count == 2


def test_iso_date_discovery_is_supported():
    client, _ = client_for(discovery=[{"usage_date": str(END)}])
    assert fetch(client).end == END


@pytest.mark.parametrize("value", [None, -1, source.MAX_BYTES + 1, 1.5, True])
@pytest.mark.parametrize("actual", [False, True])
def test_unknown_or_overbudget_bytes_fail_closed(value, actual):
    client, jobs = client_for()
    jobs[int(actual)].total_bytes_processed = value
    with pytest.raises(ValueError, match="actual bytes" if actual else "estimate"):
        fetch(client)
    assert client.query.call_count == (2 if actual else 1)


def test_exact_byte_budget_is_permitted():
    client, jobs = client_for()
    for job in jobs:
        job.total_bytes_processed = source.MAX_BYTES
    assert fetch(client).end == END


@pytest.mark.parametrize("field,value,match", [
    ("table_type", "VIEW", "permanent"), ("table_type", "EXTERNAL", "permanent"),
    ("expires", datetime(2026, 12, 1), "non-expiring"),
    ("time_partitioning", SimpleNamespace(expiration_ms=1000), "non-expiring"),
    ("modified", None, "watermark"), ("num_rows", None, "watermark"),
    ("num_rows", -1, "watermark"), ("num_rows", True, "watermark"),
    ("etag", None, "watermark"), ("etag", "", "watermark"),
])
def test_source_metadata_requires_permanent_complete_snapshot(field, value, match):
    client, _ = client_for()
    setattr(client.get_table.return_value, field, value)
    with pytest.raises(ValueError, match=match):
        fetch(client)
    client.query.assert_not_called()


def test_partitioned_table_with_no_ttl_and_zero_rows_is_valid_metadata():
    client, _ = client_for()
    client.get_table.return_value.time_partitioning = SimpleNamespace(expiration_ms=None)
    client.get_table.return_value.num_rows = 0
    assert fetch(client).end == END


@pytest.mark.parametrize("changed_field,value", [
    ("modified", datetime(2026, 11, 5, tzinfo=timezone.utc)),
    ("num_rows", 101), ("etag", "changed"),
])
@pytest.mark.parametrize("at_call,queries", [(2, 0), (3, 2), (7, 6)])
def test_snapshot_changes_before_or_after_any_read_fail_closed(changed_field, value, at_call, queries):
    client, _ = client_for()
    count = 0

    def get_table(*args, **kwargs):
        nonlocal count
        count += 1
        table = metadata()
        if count >= at_call:
            setattr(table, changed_field, value)
        return table

    client.get_table.side_effect = get_table
    with pytest.raises(ValueError, match="snapshot changed"):
        fetch(client)
    assert client.query.call_count == queries


def test_missing_azure_month_aborts_before_query_submission():
    client, _ = client_for()
    client.get_table.side_effect = [metadata(), FileNotFoundError("missing next month")]
    with pytest.raises(FileNotFoundError, match="next month"):
        fetch(client, "azure")
    client.query.assert_not_called()


def test_assert_snapshot_can_be_reused_by_writer():
    client, _ = client_for()
    window = fetch(client)
    source.assert_snapshot(client, window.snapshots)
    client.get_table.return_value.etag = "changed"
    with pytest.raises(ValueError, match="snapshot changed"):
        source.assert_snapshot(client, window.snapshots)


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_result_errors_cancel_once_without_retry_or_masking_original(cancel_fails):
    client, jobs = client_for()
    jobs[1].result.side_effect = TimeoutError("query timed out")
    if cancel_fails:
        jobs[1].cancel.side_effect = RuntimeError("cancel failed too")
    with pytest.raises(TimeoutError, match="query timed out"):
        fetch(client)
    jobs[1].cancel.assert_called_once_with(timeout=30, retry=None)
    assert client.query.call_count == 2


def test_page_iteration_is_also_deadline_bounded(monkeypatch):
    client, jobs = client_for()
    monkeypatch.setattr(source.time, "monotonic", Mock(side_effect=[0, 901]))
    with pytest.raises(TimeoutError, match="collection exceeded 900"):
        fetch(client)
    jobs[1].cancel.assert_called_once_with(timeout=30, retry=None)
    assert client.query.call_count == 2


def test_iteration_fetches_all_pages_in_order():
    client, jobs = client_for()
    jobs[5].result.return_value = iter([{"page": 1}, {"page": 2}, {"page": 3}])
    assert fetch(client).rows == [{"page": 1}, {"page": 2}, {"page": 3}]


@pytest.mark.parametrize("changes,match", [({"project": "bad.project`"}, "project"),
    ({"project": ""}, "project"), ({"project": None}, "project"),
    ({"vendor": "gcp"}, "vendor"), ({"account_id": ""}, "account"),
    ({"account_id": " account "}, "account"), ({"account_id": None}, "account"),
    ({"today": datetime(2026, 11, 4)}, "UTC date")])
def test_invalid_input_fails_before_any_bigquery_access(changes, match):
    client, _ = client_for()
    with pytest.raises(ValueError, match=match):
        fetch(client, **changes)
    client.query.assert_not_called()
    client.get_table.assert_not_called()


@pytest.mark.parametrize("vendor", ["aws", "azure"])
def test_changed_shared_query_builder_contract_fails_closed(monkeypatch, vendor):
    name = f"build_{vendor}_billing_summary_query"
    monkeypatch.setattr(source, name, Mock(return_value="SELECT unexpected"))
    client, _ = client_for()
    with pytest.raises(ValueError, match="query-builder contract"):
        fetch(client, vendor)
    assert client.query.call_count == 4
