"""Read-only, bounded account-level cost watermarks (not completeness checks)."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from ci_dashboard.common.config import CostFreshnessSettings


@dataclass
class CostSourceResult:
    vendor: str
    account_id: str
    source_latest: date | None = None
    eligible_latest: date | None = None
    summary_latest: date | None = None
    attribution_latest: date | None = None
    status: str = "query_error"
    detail: str = ""
    scope: str = "account"

    @property
    def passed(self) -> bool | None:
        if self.status in {"excluded", "non_bigquery", "waiting_import_lag"}:
            return None
        return self.status in {"ok", "within_import_lag"}

    @property
    def level(self) -> str:
        return "LOW" if self.status == "scope_unverified" else "MEDIUM"

    def describe(self) -> str:
        def diff(left: date | None, right: date | None) -> str:
            return f"{(left - right).days:+d}d" if left and right else "n/a"

        return (
            f"{self.vendor}/{self.account_id}: {self.status}; "
            f"BQ raw={self.source_latest}, eligible={self.eligible_latest}; "
            f"summary={self.summary_latest}, attr={self.attribution_latest}; "
            f"raw-summary={diff(self.source_latest, self.summary_latest)}, "
            f"eligible-summary={diff(self.eligible_latest, self.summary_latest)}, "
            f"summary-attr={diff(self.summary_latest, self.attribution_latest)}; "
            f"scope={self.scope}; {self.detail}"
        )


def _date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value))


# Constant SQL only: account identifiers are bound, never table identifiers.
_SUMMARY = text("""
    SELECT account_id, MAX(usage_date) AS latest
    FROM cost_bq_export_summary_daily
    WHERE vendor IN (:vendor, :upper_vendor) AND account_id IN :accounts
      AND usage_date BETWEEN :start AND :end
    GROUP BY account_id
""").bindparams(bindparam("accounts", expanding=True))
_ATTRIBUTION = text("""
    SELECT account_id, MAX(usage_date) AS latest
    FROM cost_attribution_daily
    WHERE vendor IN (:vendor, :upper_vendor) AND account_id IN :accounts
      AND usage_date BETWEEN :start AND :end
    GROUP BY account_id
""").bindparams(bindparam("accounts", expanding=True))


def _source_sql(vendor: str, project: str) -> str:
    if vendor == "aws":
        return f"""
            SELECT account_id, MAX(usage_date) AS source_latest,
                   MAX(usage_date) AS eligible_latest
            FROM `{project}.mart_cloud_cost.aws_member_daily_cost`
            WHERE billing_period BETWEEN @month_start AND @month_end
              AND usage_date BETWEEN @usage_start AND @usage_end
              AND account_id IN UNNEST(@accounts) AND currency = 'USD'
            GROUP BY account_id
        """
    if vendor == "gcp":
        return f"""
            SELECT project.id AS account_id, MAX(DATE(usage_start_time)) AS source_latest,
                   MAX(IF(_PARTITIONDATE <= @eligible_export_end,
                          DATE(usage_start_time), NULL)) AS eligible_latest
            FROM `{project}.raw_gcp_billing.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6`
            WHERE _PARTITIONDATE BETWEEN @export_start AND @export_end
              AND DATE(usage_start_time) BETWEEN @usage_start AND @usage_end
              AND project.id IN UNNEST(@accounts)
            GROUP BY account_id
        """
    if vendor == "azure":
        return f"""
            SELECT SubscriptionId AS account_id, MAX(date) AS source_latest,
                   MAX(IF(_TABLE_SUFFIX <= @eligible_month_suffix, date, NULL)) AS eligible_latest
            FROM `{project}.raw_azure_billing.azure_billing_cost_*`
            WHERE _TABLE_SUFFIX BETWEEN @month_start_suffix AND @month_end_suffix
              AND REGEXP_CONTAINS(_TABLE_SUFFIX, r'^[0-9]{{8}}$')
              AND date BETWEEN @usage_start AND @usage_end
              AND SubscriptionId IN UNNEST(@accounts)
            GROUP BY account_id
        """
    if vendor == "alibaba":
        return f"""
            SELECT CAST(owner_account_id AS STRING) AS account_id,
                   MAX(DATE(usage_start_time)) AS source_latest,
                   MAX(IF(partition_date <= @eligible_export_end,
                          DATE(usage_start_time), NULL)) AS eligible_latest
            FROM `{project}.raw_alibaba_billing.daily_en_*`
            WHERE _TABLE_SUFFIX BETWEEN @export_start_suffix AND @export_end_suffix
              AND REGEXP_CONTAINS(_TABLE_SUFFIX, r'^[0-9]{{8}}$')
              AND partition_date BETWEEN @export_start AND @export_end
              AND DATE(usage_start_time) BETWEEN @usage_start AND @usage_end
              AND CAST(owner_account_id AS STRING) IN UNNEST(@accounts)
            GROUP BY account_id
        """
    raise ValueError(f"Unknown BigQuery vendor: {vendor}")


def _query_source(client, vendor, accounts, settings, start, today):
    from google.cloud import bigquery

    month_start, month_end = start.replace(day=1), today.replace(day=1)
    export_start = start
    if vendor == "alibaba":
        export_start = max(
            (month_start - timedelta(days=1)).replace(day=1), today - timedelta(days=61)
        )
    # 09:10 CST precedes today's daily importer. Lag applies to export dates,
    # or readable export months for Azure, NOT to the usage-date upper bound.
    eligible_end = today - timedelta(
        days=1 + {"aws": 0, "gcp": 3, "azure": 3, "alibaba": 5}[vendor]
    )
    values = {
        "usage_start": start,
        "usage_end": today,
        "month_start": month_start,
        "month_end": month_end,
        "export_start": export_start,
        "export_end": today,
        "eligible_export_end": eligible_end,
        "month_start_suffix": month_start.strftime("%Y%m%d"),
        "month_end_suffix": month_end.strftime("%Y%m%d"),
        "eligible_month_suffix": eligible_end.replace(day=1).strftime("%Y%m%d"),
        "export_start_suffix": export_start.strftime("%Y%m%d"),
        "export_end_suffix": today.strftime("%Y%m%d"),
    }
    sql = _source_sql(vendor, settings.project)
    used = set(re.findall(r"@([A-Za-z0-9_]+)", sql))
    parameters = [bigquery.ArrayQueryParameter("accounts", "STRING", accounts)]
    parameters.extend(
        bigquery.ScalarQueryParameter(name, "DATE" if isinstance(value, date) else "STRING", value)
        for name, value in values.items()
        if name in used
    )
    cap = settings.maximum_bytes_billed
    kwargs = {
        "location": settings.location,
        "timeout": settings.timeout_seconds,
        "retry": None,
        "job_retry": None,
    }
    dry = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=True,
            maximum_bytes_billed=cap,
            query_parameters=parameters,
        ),
        **kwargs,
    )
    if dry.total_bytes_processed is None or dry.total_bytes_processed > cap:
        raise ValueError(f"dry-run estimate {dry.total_bytes_processed} exceeds/unknown cap {cap}")
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            use_query_cache=True,
            maximum_bytes_billed=cap,
            query_parameters=parameters,
        ),
        **kwargs,
    )
    return {
        str(row["account_id"]): (_date(row["source_latest"]), _date(row["eligible_latest"]))
        for row in job.result(timeout=settings.timeout_seconds, retry=None, job_retry=None)
    }


def _classify(result: CostSourceResult, today: date) -> None:
    source, eligible = result.source_latest, result.eligible_latest
    summary, attr = result.summary_latest, result.attribution_latest
    if result.scope != "account":
        # The upstream scope is unverified, but a local lineage gap is still real.
        if summary is not None and attr is None:
            result.status = "missing_attribution"
        elif summary is not None and attr is not None and attr != summary:
            result.status = "attribution_delay" if attr < summary else "attribution_ahead"
        else:
            result.status = "scope_unverified"
    elif source is None:
        result.status = "source_empty"
    elif any(local and local > source for local in (summary, attr)):
        result.status = "local_ahead"
    elif eligible is None and summary is None and attr is None:
        result.status = "waiting_import_lag"
    elif summary is None:
        result.status = "missing_summary"
    elif attr is None:
        result.status = "missing_attribution"
    elif attr > summary:
        result.status = "attribution_ahead"
    elif eligible is not None and (eligible - summary).days > 1:
        result.status = "ingestion_delay"
    elif attr < summary:
        result.status = "attribution_delay"
    elif (today - source).days > 7:
        result.status = (
            "source_stale_with_import_gap"
            if summary < source
            else "source_stale_or_no_recent_usage"
        )
    elif eligible is None:
        result.status = "waiting_import_lag"
    elif source > eligible and summary < source:
        result.status = "within_import_lag"
    else:
        result.status = "ok"


def run_cost_source_checks(
    engine: Engine,
    settings: CostFreshnessSettings,
    *,
    client=None,
    today: date | None = None,
) -> list[CostSourceResult]:
    """Compare every active registry identity; failures never escape into legacy checks."""
    if not settings.enabled:
        return []
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=settings.lookback_days - 1)
    results = []
    groups = defaultdict(list)
    try:
        with engine.connect() as conn:
            sources = (
                conn.execute(
                    text("""
                SELECT vendor, account_id, display_name, source_schema_version, source_available_from
                FROM cost_sources WHERE is_active = 1 ORDER BY vendor, account_id
            """)
                )
                .mappings()
                .all()
            )
            for row in sources:
                vendor, account = str(row["vendor"]).lower(), str(row["account_id"])
                result = CostSourceResult(vendor, account)
                results.append(result)
                profile = row["source_schema_version"]
                result.detail = (
                    f"profile={profile or 'default'}; available_from={row['source_available_from']}; "
                    f"usage window={start}..{today}; "
                    "MAX watermarks only, not continuity/completeness"
                )
                if f"{vendor}/{account}" in settings.excluded_sources:
                    result.status, result.detail = (
                        "excluded",
                        "Explicitly excluded by configuration",
                    )
                elif vendor == "tencent":
                    result.status, result.detail = (
                        "non_bigquery",
                        "Tencent uses an API, not BigQuery",
                    )
                elif vendor not in {"aws", "gcp", "azure", "alibaba"}:
                    result.status, result.scope = "scope_unverified", "unknown_vendor"
                else:
                    if profile and not (vendor == "aws" and profile == "aws_cur_legacy_v1"):
                        result.scope = "accountwide_diagnostic_only"
                        result.detail += "; account-wide dates do not verify this profile's scope"
                    groups[vendor].append(result)
            for vendor, group in groups.items():
                params = {
                    "vendor": vendor,
                    "upper_vendor": vendor.upper(),
                    "accounts": [r.account_id for r in group],
                    "start": start,
                    "end": today,
                }
                try:
                    for statement, field in (
                        (_SUMMARY, "summary_latest"),
                        (_ATTRIBUTION, "attribution_latest"),
                    ):
                        for row in conn.execute(statement, params).mappings():
                            for result in group:
                                if result.account_id == str(row["account_id"]):
                                    setattr(result, field, _date(row["latest"]))
                except Exception as exc:  # noqa: BLE001 - isolate each vendor's local SQL
                    for result in group:
                        result.status, result.detail = (
                            "local_query_error",
                            f"Local SQL failed: {exc}",
                        )
    except Exception as exc:  # noqa: BLE001 - legacy checks must survive registry failures
        return [
            CostSourceResult(
                "registry",
                "*",
                status="registry_error",
                detail=f"Active source registry failed: {exc}",
            )
        ]
    # No DB connection is held while ADC or BigQuery can block.
    pending = {
        vendor: group
        for vendor, group in groups.items()
        if all(r.status != "local_query_error" for r in group)
    }
    if not pending:
        return results or [
            CostSourceResult(
                "registry", "*", status="registry_empty", detail="No active cost sources"
            )
        ]
    try:
        if not settings.project or not re.fullmatch(
            r"[a-z][a-z0-9-]{4,61}[a-z0-9]", settings.project
        ):
            raise ValueError("An explicit valid GCP project ID is required")
        if not settings.location or not settings.location.strip():
            raise ValueError("An explicit BigQuery location is required")
        if (
            min(settings.lookback_days, settings.timeout_seconds, settings.maximum_bytes_billed)
            <= 0
        ):
            raise ValueError("Lookback, timeout and byte cap must be positive")
        if client is None:
            from google.cloud import bigquery

            client = bigquery.Client(project=settings.project, location=settings.location)
    except Exception as exc:  # noqa: BLE001 - ADC/config errors become report entries
        for group in pending.values():
            for result in group:
                result.status, result.detail = "client_error", f"BigQuery setup failed: {exc}"
        return results
    for vendor, group in pending.items():
        try:
            latest = _query_source(
                client, vendor, [r.account_id for r in group], settings, start, today
            )
            for result in group:
                result.source_latest, result.eligible_latest = latest.get(
                    result.account_id, (None, None)
                )
                _classify(result, today)
        except Exception as exc:  # noqa: BLE001 - one vendor must not hide other results
            for result in group:
                result.status, result.detail = "query_error", f"BigQuery {vendor} failed: {exc}"
    return results
