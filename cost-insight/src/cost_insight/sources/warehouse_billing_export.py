"""Fail-closed warehouse reads, including the explicitly bounded F04 tenant scope."""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from google.cloud import bigquery

from cost_insight.sources.aws_billing_export import build_aws_billing_summary_query
from cost_insight.sources.aws_flat_cur_export import (
    _AWS_TIDB_CLOUD_F04_USEDBY,
    build_aws_flat_cur_summary_query,
)
from cost_insight.sources.azure_billing_export import (
    azure_warehouse_tables,
    build_azure_billing_summary_query,
    build_azure_warehouse_relation,
)

LOG = logging.getLogger(__name__)
MAX_BYTES = 2 * 1024**3
# Complete F04 SKU detail is larger than ordinary account reads; never raise their cap.
F04_MAX_BYTES = 32 * 1024**3
F04_ACCOUNT_ID = "380838443567"
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
Snapshot = tuple[str, int, str]


@dataclass(frozen=True)
class WarehouseWindow:
    start: date
    end: date
    rows: Sequence[dict[str, Any]]
    controls: Sequence[dict[str, Any]]
    snapshots: Mapping[str, Snapshot]


def _log(event: str, **fields) -> None:
    LOG.info("%s", json.dumps({"event": event, **fields}, default=str, sort_keys=True))


def source_table(vendor: str, project: str) -> str:
    if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
        raise ValueError("Invalid warehouse BigQuery project")
    if vendor == "aws":
        return f"{project}.raw_aws_billing.cur_line_items"
    if vendor == "azure":
        return f"{project}.raw_azure_billing.azure_billing_cost_*"
    raise ValueError("Warehouse vendor must be aws or azure")


def _control_query(vendor: str, table: str, start: date, end: date, *, f04: bool = False) -> str:
    if f04:
        return f"""
SELECT DATE(bill_billing_period_start_date) AS export_partition_date,
       DATE(line_item_usage_start_date) AS usage_date, COUNT(*) AS n,
       COUNTIF(line_item_currency_code IS NULL OR line_item_currency_code != 'USD') AS non_usd,
       SUM(CAST(COALESCE(line_item_unblended_cost, 0) AS BIGNUMERIC)) AS list_cost
FROM `{table}` AS raw
WHERE bill_billing_period_start_date >= TIMESTAMP(@export_partition_start)
  AND bill_billing_period_start_date < TIMESTAMP(DATE_ADD(@export_partition_end, INTERVAL 1 DAY))
  AND line_item_usage_account_id = @account_id
  AND DATE(line_item_usage_start_date) BETWEEN @earliest_usage_date AND @usage_end_date
  AND line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')
  AND (SELECT kv.value FROM UNNEST(raw.resource_tags.key_value) AS kv
       WHERE kv.key = 'user_usedby' LIMIT 1) = @usedby
GROUP BY export_partition_date, usage_date
""".strip()
    if vendor == "aws":
        return f"""
SELECT DATE(bill_billing_period_start_date) AS export_partition_date,
       DATE(line_item_usage_start_date) AS usage_date, COUNT(*) AS n,
       COUNTIF(line_item_currency_code IS NULL OR line_item_currency_code != 'USD') AS non_usd,
       SUM(CASE WHEN line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage')
                THEN COALESCE(line_item_unblended_cost, 0) ELSE 0 END) AS list_cost
FROM `{table}`
WHERE bill_billing_period_start_date >= TIMESTAMP(@export_partition_start)
  AND bill_billing_period_start_date < TIMESTAMP(DATE_ADD(@export_partition_end, INTERVAL 1 DAY))
  AND line_item_usage_account_id = @account_id
  AND DATE(line_item_usage_start_date) BETWEEN @earliest_usage_date AND @usage_end_date
GROUP BY export_partition_date, usage_date
""".strip()
    return f"""
SELECT SAFE.PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) AS export_partition_date,
       CAST(date AS DATE) AS usage_date, COUNT(*) AS n,
       COUNTIF(billingCurrency IS NULL OR billingCurrency != 'USD') AS non_usd,
       COUNTIF(SAFE_CAST(paygCostInBillingCurrency AS NUMERIC) IS NULL
            OR SAFE_CAST(costInBillingCurrency AS NUMERIC) IS NULL) AS invalid_money,
       SUM(SAFE_CAST(paygCostInBillingCurrency AS NUMERIC)) AS list_cost,
       SUM(SAFE_CAST(costInBillingCurrency AS NUMERIC)) AS effective_cost,
       CAST(0 AS NUMERIC) AS credit_amount,
       SUM(SAFE_CAST(costInBillingCurrency AS NUMERIC)) AS net_cost
FROM {build_azure_warehouse_relation(table, start, end)}
WHERE CAST(SubscriptionId AS STRING) = @account_id
GROUP BY export_partition_date, usage_date
""".strip()


def _summary_query(vendor: str, table: str, start: date, end: date, *, f04: bool = False) -> str:
    if f04:
        return build_aws_flat_cur_summary_query(
            billing_table=table, export_partition_start=start.replace(day=1),
            export_partition_end=end.replace(day=1), usage_end_date=end, warehouse_raw=True)
    if vendor == "aws":
        return build_aws_billing_summary_query(billing_table=table, usage_end_date=end,
                                               warehouse_raw=True, usd_only=True)
    return build_azure_billing_summary_query(billing_table=table, warehouse_window=(start, end))


def _parameters(account: str, start: date, end: date, sql: str, *, billing_month: date | None = None):
    return [bigquery.ScalarQueryParameter(name, kind, value) for name, kind, value in (
        ("account_id", "STRING", account),
        ("usedby", "STRING", _AWS_TIDB_CLOUD_F04_USEDBY),
        ("export_partition_start", "DATE", billing_month or start.replace(day=1)),
        ("export_partition_end", "DATE", billing_month or end.replace(day=1)),
        ("earliest_usage_date", "DATE", start),
        ("usage_end_date", "DATE", end),
    ) if f"@{name}" in sql]


def _snapshot(client, tables) -> dict[str, Snapshot]:
    result = {}
    for table_id in tables:
        table = client.get_table(table_id, timeout=30, retry=None)
        if (table.table_type != "TABLE" or table.expires is not None
                or (table.time_partitioning and table.time_partitioning.expiration_ms)):
            raise ValueError("Warehouse source must be permanent, non-expiring physical tables")
        if (table.modified is None or type(table.num_rows) is not int or table.num_rows < 0
                or not table.etag):
            raise ValueError("Warehouse source has an incomplete modification watermark")
        result[table_id] = (table.modified.isoformat(), table.num_rows, table.etag)
    return result


def assert_snapshot(client, snapshots: Mapping[str, Snapshot]) -> None:
    if _snapshot(client, snapshots) != snapshots:
        raise ValueError("Warehouse source snapshot changed; refusing torn reads or further writes")


def _query_rows(client, sql: str, params, *, label: str, heartbeat: Callable[[], None],
                snapshots: Mapping[str, Snapshot], maximum_bytes: int = MAX_BYTES) -> list[dict[str, Any]]:
    """Every read shares metadata guards, an independent dry-run and fixed execution bounds."""
    heartbeat()
    assert_snapshot(client, snapshots)
    options = dict(query_parameters=params, maximum_bytes_billed=maximum_bytes, use_query_cache=False)
    preview = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, **options),
                           timeout=30, retry=None, job_retry=None)
    estimate = preview.total_bytes_processed
    _log("query_preflight", query=label, estimated_bytes=estimate,
         maximum_bytes_billed=maximum_bytes, job_id=preview.job_id)
    if type(estimate) is not int or not 0 <= estimate <= maximum_bytes:
        raise ValueError(f"Unknown/over-budget estimate for {label}: {estimate}")
    job = client.query(sql, job_config=bigquery.QueryJobConfig(**options),
                       timeout=30, retry=None, job_retry=None)
    started = time.monotonic()
    try:
        rows = []
        for row in job.result(page_size=1000, timeout=900, retry=None, job_retry=None):
            if time.monotonic() - started > 900:
                raise TimeoutError(f"Result collection exceeded 900 seconds: {label}")
            heartbeat()
            rows.append(dict(row.items()))
    except BaseException:
        try:
            job.cancel(timeout=30, retry=None)
        except Exception:
            LOG.exception("Could not cancel warehouse query %s (%s)", label, job.job_id)
        raise
    heartbeat()
    actual = job.total_bytes_processed
    _log("query_fetched", query=label, rows=len(rows), bytes_processed=actual, job_id=job.job_id)
    if type(actual) is not int or not 0 <= actual <= maximum_bytes:
        raise ValueError(f"Unknown/over-budget actual bytes for {label}: {actual}")
    assert_snapshot(client, snapshots)
    return rows


def _date(value) -> date:
    if isinstance(value, datetime):
        raise ValueError("Expected a warehouse usage date, not a timestamp")
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def fetch_warehouse_window(
    client, *, vendor: str, account_id: str, project: str, today: date,
    heartbeat: Callable[[], None], f04: bool = False,
    start_date: date | None = None, end_date: date | None = None,
) -> WarehouseWindow:
    """Read a complete trailing window, or an explicit 1..5-day F04 repair window."""
    table = source_table(vendor, project)
    if not isinstance(account_id, str) or not account_id or account_id.strip() != account_id:
        raise ValueError("An explicit nonempty warehouse account ID is required")
    if type(today) is not date:
        raise ValueError("today must be a UTC date")
    if type(f04) is not bool or (f04 and (vendor != "aws" or account_id != F04_ACCOUNT_ID)):
        raise ValueError("F04 warehouse reads require the exact AWS F04 account")
    if vendor == "aws" and account_id == F04_ACCOUNT_ID and not f04:
        raise ValueError("F04 cannot be queried as an ordinary account-wide source")
    if (start_date is None) != (end_date is None):
        raise ValueError("start_date and end_date must be set together")
    yesterday = today - timedelta(days=1)
    if start_date is not None:
        if (not f04 or type(start_date) is not date or type(end_date) is not date
                or not 0 <= (end_date - start_date).days <= 4 or end_date > yesterday):
            raise ValueError("Historical F04 reads require 1..5 inclusive past usage days")
    lookup_start = start_date if start_date is not None else yesterday - timedelta(days=13)
    lookup_end = end_date if end_date is not None else yesterday
    tables = (table,) if vendor == "aws" else azure_warehouse_tables(table, lookup_start, lookup_end).values()
    heartbeat()
    snapshots = _snapshot(client, tables)
    _log("source_snapshot", vendor=vendor, account_id=account_id, tables=snapshots)

    def read(start: date, end: date, label: str, *, summary=False):
        build = _summary_query if summary else _control_query
        sql = build(vendor, table, start, end, **({"f04": True} if f04 else {}))
        if not f04:
            return _query_rows(client, sql, _parameters(account_id, start, end, sql),
                               label=f"{vendor}/{account_id}/{label}", heartbeat=heartbeat,
                               snapshots=snapshots)
        # Physical billing-month bounds keep full-SKU reads independent and budgeted.
        rows = []
        month, last_month = start.replace(day=1), end.replace(day=1)
        while month <= last_month:
            monthly_rows = _query_rows(client, sql,
                _parameters(account_id, start, end, sql, billing_month=month),
                label=f"aws/{account_id}/f04/{label}/{month}", heartbeat=heartbeat,
                snapshots=snapshots, maximum_bytes=F04_MAX_BYTES)
            # The existing F04 builder filters USD but omits its constant output column.
            rows.extend({"currency": "USD", **row} if summary else row for row in monthly_rows)
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        return rows

    if start_date is not None:
        controls = read(start_date, end_date, "control")
        if not controls:
            raise ValueError("No actual usage in explicit F04 repair window")
        rows = read(start_date, end_date, "summary", summary=True)
        return WarehouseWindow(start_date, end_date, rows, controls, snapshots)

    discovery = read(lookup_start, yesterday, "discover")
    if not discovery:
        raise ValueError(f"No actual usage in bounded 14-day discovery: {vendor}/{account_id}")
    end = max(_date(row["usage_date"]) for row in discovery)
    if not lookup_start <= end <= yesterday:
        raise ValueError("Discovery usage date is outside the bounded source window")
    if (today - end).days > 7:
        raise ValueError(f"Warehouse source is stale: {vendor}/{account_id}/{end}")
    start = end - timedelta(days=4)
    # Discovery already grouped the raw control by the same partition/day keys.
    controls = [row for row in discovery if start <= _date(row["usage_date"]) <= end
                and start.replace(day=1) <= _date(row["export_partition_date"]) <= end.replace(day=1)]
    rows = read(start, end, "summary", summary=True)
    _log("source_window_fetched", vendor=vendor, account_id=account_id,
         start=start, end=end, rows=len(rows))
    return WarehouseWindow(start, end, rows, controls, snapshots)
