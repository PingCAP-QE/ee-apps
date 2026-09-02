from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from cost_insight.common.config import DEFAULT_AWS_BILLING_TABLE
from cost_insight.jobs.cost_sources import get_cost_source
from cost_insight.sources.aws_billing_export import build_aws_billing_summary_query
from cost_insight.sources.aws_split_cost_export import build_aws_split_cost_summary_query

AWS_7266_ACCOUNT_ID = "946646677266"
AWS_8728_ACCOUNT_ID = "296171618728"
AWS_7266_SPLIT_TABLE = (
    "pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
)
_PIPELINE_METRICS = ("list_cost", "effective_cost", "credit_amount", "net_cost")


@dataclass(frozen=True)
class ReconciliationSource:
    account_id: str
    billing_table: str
    schema_version: str = "aws_cur_legacy_v1"


@dataclass(frozen=True)
class ReconciliationResult:
    account_id: str
    tenant: str
    start_date: date
    end_date: date
    metric: str
    bq_raw: Decimal
    summary: Decimal
    attribution: Decimal
    cost_explorer: Decimal
    bq_raw_delta: Decimal
    summary_delta: Decimal
    cost_explorer_delta: Decimal
    passed: bool
    pipeline_metrics: tuple[dict[str, Any], ...]
    daily: tuple[dict[str, Any], ...]
    attribution_breakdown: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "tenant": self.tenant,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "metric": self.metric,
            "bq_raw": str(self.bq_raw),
            "summary": str(self.summary),
            "attribution": str(self.attribution),
            "cost_explorer": str(self.cost_explorer),
            "bq_raw_delta": str(self.bq_raw_delta),
            "summary_delta": str(self.summary_delta),
            "cost_explorer_delta": str(self.cost_explorer_delta),
            "passed": self.passed,
            "pipeline_metrics": list(self.pipeline_metrics),
            "daily": list(self.daily),
            "attribution_breakdown": list(self.attribution_breakdown),
        }


def default_reconciliation_source(
    account_id: str,
    *,
    legacy_table: str = DEFAULT_AWS_BILLING_TABLE,
) -> ReconciliationSource:
    if account_id == AWS_7266_ACCOUNT_ID:
        return ReconciliationSource(
            account_id=account_id,
            billing_table=AWS_7266_SPLIT_TABLE,
            schema_version="aws_split_cost_v1",
        )
    return ReconciliationSource(account_id=account_id, billing_table=legacy_table)


def resolve_reconciliation_source(
    engine: Engine,
    *,
    account_id: str,
    legacy_table: str = DEFAULT_AWS_BILLING_TABLE,
) -> ReconciliationSource:
    """Read the active source profile without creating or updating it."""
    with engine.begin() as connection:
        source = get_cost_source(connection, vendor="aws", account_id=account_id)
    if source is None:
        return default_reconciliation_source(account_id, legacy_table=legacy_table)
    if not source.is_active:
        raise ValueError(f"AWS cost source is inactive: {account_id}")
    schema_version = source.source_schema_version or "aws_cur_legacy_v1"
    billing_table = source.source_table or legacy_table
    if schema_version == "aws_split_cost_v1" and not source.source_table:
        raise ValueError(f"AWS split-cost source {account_id} has no source_table")
    return ReconciliationSource(
        account_id=account_id,
        billing_table=billing_table,
        schema_version=schema_version,
    )


def build_bq_reconciliation_query(
    source: ReconciliationSource,
    *,
    metric: str,
) -> str:
    """Aggregate the normalized BQ source stream, not physical CUR rows.

    In particular, the split-cost adapter replaces a parent direct row with its
    child allocations and a residual row. Filtering physical parent rows by
    tenant would not be comparable to summary or attribution after that split.
    """
    if metric not in _PIPELINE_METRICS:
        raise ValueError(f"Unsupported reconciliation metric: {metric!r}")
    if source.schema_version == "aws_split_cost_v1":
        source_query = build_aws_split_cost_summary_query(
            billing_table=source.billing_table,
            include_usage_end_date=True,
        )
    elif source.schema_version == "aws_cur_legacy_v1":
        source_query = build_aws_billing_summary_query(billing_table=source.billing_table)
    else:
        raise ValueError(f"Unsupported AWS source schema: {source.schema_version!r}")
    return f"""
WITH source_rows AS (
{source_query}
)
SELECT COALESCE(SUM(CAST({metric} AS BIGNUMERIC)), 0) AS amount
FROM source_rows
WHERE org = @tenant
  AND usage_date >= @start_date
  AND usage_date < @end_date
""".strip()


def fetch_bq_amount(
    client: Any,
    *,
    source: ReconciliationSource,
    tenant: str,
    start_date: date,
    end_date: date,
    metric: str,
) -> Decimal:
    from google.cloud import bigquery

    query = build_bq_reconciliation_query(source, metric=metric)
    export_partition_start = start_date.replace(day=1)
    export_partition_end = (end_date - timedelta(days=1)).replace(day=1)
    parameters = [
        bigquery.ScalarQueryParameter("account_id", "STRING", source.account_id),
        bigquery.ScalarQueryParameter(
            "export_partition_start", "DATE", export_partition_start.isoformat()
        ),
        bigquery.ScalarQueryParameter(
            "export_partition_end", "DATE", export_partition_end.isoformat()
        ),
        bigquery.ScalarQueryParameter("earliest_usage_date", "DATE", start_date.isoformat()),
        bigquery.ScalarQueryParameter("start_date", "DATE", start_date.isoformat()),
        bigquery.ScalarQueryParameter("end_date", "DATE", end_date.isoformat()),
        bigquery.ScalarQueryParameter("tenant", "STRING", tenant),
    ]
    if source.schema_version == "aws_split_cost_v1":
        parameters.append(
            bigquery.ScalarQueryParameter(
                "usage_end_date", "DATE", (end_date - timedelta(days=1)).isoformat()
            )
        )
    job = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=parameters
        ),
    )
    row = next(iter(job.result()), None)
    if row is None:
        return Decimal()
    value = row["amount"] if not isinstance(row, dict) else row.get("amount")
    return Decimal(str(value or 0))


def fetch_cost_explorer_amount(
    client: Any,
    *,
    account_id: str,
    tenant: str,
    start_date: date,
    end_date: date,
    tenant_tag_key: str = "tenant",
) -> Decimal:
    request = {
        "TimePeriod": {"Start": start_date.isoformat(), "End": end_date.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": {
            "And": [
                {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}},
                {
                    "Dimensions": {
                        "Key": "RECORD_TYPE",
                        "Values": ["Usage", "SavingsPlanCoveredUsage"],
                    }
                },
                {"Tags": {"Key": tenant_tag_key, "Values": [tenant]}},
            ]
        },
    }
    amount = Decimal()
    page_token = None
    while True:
        if page_token:
            request["NextPageToken"] = page_token
        response = client.get_cost_and_usage(**request)
        amount += sum(
            (
                Decimal(str(result.get("Total", {}).get("UnblendedCost", {}).get("Amount", "0")))
                for result in response.get("ResultsByTime", [])
            ),
            Decimal(),
        )
        page_token = response.get("NextPageToken")
        if not page_token:
            return amount


def fetch_tenant_amount(
    engine: Engine,
    *,
    table: str,
    account_id: str,
    tenant: str,
    start_date: date,
    end_date: date,
    metric: str,
) -> Decimal:
    if metric not in _PIPELINE_METRICS:
        raise ValueError(f"Unsupported Cost Insight metric: {metric!r}")
    query = text(
        f"""
        SELECT COALESCE(SUM({metric}), 0) AS amount
        FROM {table}
        WHERE vendor = 'aws' AND account_id = :account_id
          AND org = :tenant
          AND usage_date >= :start_date AND usage_date < :end_date
        """
    )
    with engine.begin() as connection:
        return Decimal(str(connection.execute(query, {
            "account_id": account_id,
            "tenant": tenant,
            "start_date": start_date,
            "end_date": end_date,
        }).scalar_one() or 0))


def fetch_attribution_breakdown(
    engine: Engine,
    *,
    account_id: str,
    tenant: str,
    start_date: date,
    end_date: date,
    metric: str,
) -> tuple[dict[str, Any], ...]:
    if metric not in _PIPELINE_METRICS:
        raise ValueError(f"Unsupported Cost Insight metric: {metric!r}")
    query = text(
        f"""
        SELECT usage_date, project, owner, service, service_exec_id, allocate_method,
               attribution_source, attribution_status, COALESCE(SUM({metric}), 0) AS amount
        FROM cost_attribution_daily
        WHERE vendor = 'aws' AND account_id = :account_id
          AND org = :tenant
          AND usage_date >= :start_date AND usage_date < :end_date
        GROUP BY usage_date, project, owner, service, service_exec_id, allocate_method,
                 attribution_source, attribution_status
        ORDER BY usage_date, amount DESC
        """
    )
    with engine.begin() as connection:
        return tuple(
            {
                **dict(row),
                "usage_date": (
                    row["usage_date"].isoformat()
                    if hasattr(row["usage_date"], "isoformat")
                    else str(row["usage_date"])
                ),
                "amount": str(row["amount"]),
            }
            for row in connection.execute(query, {
                "account_id": account_id,
                "tenant": tenant,
                "start_date": start_date,
                "end_date": end_date,
            }).mappings()
        )


def run_aws_reconciliation(
    engine: Engine,
    *,
    bq_client: Any,
    ce_client: Any,
    source: ReconciliationSource,
    tenant: str,
    start_date: date,
    end_date: date,
    tenant_tag_key: str = "tenant",
    _include_daily: bool = True,
) -> ReconciliationResult:
    if start_date >= end_date:
        raise ValueError("start_date must be before end_date")
    # The split adapter conserves the CE-compatible direct unblended stream in
    # list_cost: allocated children plus the parent residual equal the parent
    # direct cost. effective_cost deliberately includes broader CUR record
    # types, so only list_cost is compared with Cost Explorer below.
    metric = "list_cost"
    cents = Decimal("0.01")

    def rounded(value: Decimal) -> Decimal:
        return value.quantize(cents, rounding=ROUND_HALF_UP)
    pipeline_metrics = []
    for pipeline_metric in _PIPELINE_METRICS:
        raw_amount = fetch_bq_amount(
            bq_client, source=source, tenant=tenant, start_date=start_date,
            end_date=end_date, metric=pipeline_metric,
        )
        summary_amount = fetch_tenant_amount(
            engine, table="cost_bq_export_summary_daily", account_id=source.account_id,
            tenant=tenant, start_date=start_date, end_date=end_date, metric=pipeline_metric,
        )
        attribution_amount = fetch_tenant_amount(
            engine, table="cost_attribution_daily", account_id=source.account_id,
            tenant=tenant, start_date=start_date, end_date=end_date, metric=pipeline_metric,
        )
        pipeline_metrics.append({
            "metric": pipeline_metric,
            "bq_raw": str(raw_amount),
            "summary": str(summary_amount),
            "attribution": str(attribution_amount),
            "bq_raw_delta": str(summary_amount - raw_amount),
            "summary_delta": str(attribution_amount - summary_amount),
            "passed": rounded(summary_amount) == rounded(raw_amount)
            and rounded(attribution_amount) == rounded(summary_amount),
        })
    list_cost = next(item for item in pipeline_metrics if item["metric"] == metric)
    bq_raw = Decimal(list_cost["bq_raw"])
    summary = Decimal(list_cost["summary"])
    attribution = Decimal(list_cost["attribution"])
    cost_explorer = fetch_cost_explorer_amount(
        ce_client, account_id=source.account_id, tenant=tenant,
        start_date=start_date, end_date=end_date, tenant_tag_key=tenant_tag_key,
    )
    # Compare the independently rounded amounts, rather than rounding their
    # difference: $1.004 and $0.996 are both $1.00 for this report.
    bq_raw_delta = summary - bq_raw
    summary_delta = attribution - summary
    cost_explorer_delta = cost_explorer - bq_raw
    passed = (
        all(item["passed"] for item in pipeline_metrics)
        and rounded(cost_explorer) == rounded(bq_raw)
    )
    daily_record = {
        "usage_date": start_date.isoformat(),
        "account_id": source.account_id,
        "tenant": tenant,
        "bq_raw": str(bq_raw),
        "summary": str(summary),
        "attribution": str(attribution),
        "cost_explorer": str(cost_explorer),
        "bq_raw_delta": str(bq_raw_delta),
        "summary_delta": str(summary_delta),
        "cost_explorer_delta": str(cost_explorer_delta),
        "pipeline_metrics": tuple(pipeline_metrics),
        "passed": passed,
    }
    if _include_daily and end_date - start_date > timedelta(days=1):
        daily = tuple(
            run_aws_reconciliation(
                engine,
                bq_client=bq_client,
                ce_client=ce_client,
                source=source,
                tenant=tenant,
                start_date=start_date + timedelta(days=offset),
                end_date=start_date + timedelta(days=offset + 1),
                tenant_tag_key=tenant_tag_key,
                _include_daily=False,
            ).daily[0]
            for offset in range((end_date - start_date).days)
        )
        passed = passed and all(record["passed"] for record in daily)
    else:
        daily = (daily_record,)
    return ReconciliationResult(
        account_id=source.account_id,
        tenant=tenant,
        start_date=start_date,
        end_date=end_date,
        metric=metric,
        bq_raw=bq_raw,
        summary=summary,
        attribution=attribution,
        cost_explorer=cost_explorer,
        bq_raw_delta=bq_raw_delta,
        summary_delta=summary_delta,
        cost_explorer_delta=cost_explorer_delta,
        passed=passed,
        pipeline_metrics=tuple(pipeline_metrics),
        daily=daily,
        attribution_breakdown=(
            fetch_attribution_breakdown(
                engine, account_id=source.account_id, tenant=tenant,
                start_date=start_date, end_date=end_date, metric=metric,
            )
            if _include_daily else ()
        ),
    )
