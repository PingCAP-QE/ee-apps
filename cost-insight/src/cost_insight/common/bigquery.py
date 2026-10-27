from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cost_insight.common.config import BillingBigQuerySettings, load_settings


def build_billing_bigquery_client(settings: BillingBigQuerySettings | None = None) -> Any:
    """Configure billing reads without changing GCS cache clients or requiring a DB."""
    from google.cloud import bigquery

    resolved = settings or load_settings(require_database=False).billing_bigquery
    kwargs: dict[str, Any] = {}
    if resolved.project:
        kwargs["project"] = resolved.project
    if resolved.location:
        kwargs["location"] = resolved.location
    if resolved.impersonate_service_account:
        import google.auth
        from google.auth import impersonated_credentials

        source_credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        kwargs["credentials"] = impersonated_credentials.Credentials(
            source_credentials=source_credentials,
            target_principal=resolved.impersonate_service_account,
            target_scopes=["https://www.googleapis.com/auth/bigquery"],
            lifetime=900,
            quota_project_id=resolved.project,
        )
    return bigquery.Client(**kwargs)


class BigQueryExecutionError(RuntimeError):
    """Raised when a BigQuery query cannot be executed successfully."""


@dataclass(frozen=True)
class BigQueryParameter:
    name: str
    type_: str
    value: Any


@dataclass(frozen=True)
class BigQueryQueryResult:
    rows: tuple[dict[str, Any], ...]
    total_bytes_processed: int | None


def execute_query(
    query: str,
    *,
    parameters: Sequence[BigQueryParameter] = (),
) -> BigQueryQueryResult:
    from google.cloud import bigquery

    client = bigquery.Client()
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(param.name, param.type_, param.value) for param in parameters
        ]
    )
    try:
        job = client.query(query, job_config=job_config)
        rows = tuple(dict(row.items()) for row in job.result())
    except Exception as exc:
        parameter_names = ", ".join(param.name for param in parameters) or "none"
        raise BigQueryExecutionError(
            f"BigQuery query failed ({type(exc).__name__}) with parameters [{parameter_names}]: {exc}"
        ) from exc
    total_bytes_processed = getattr(job, "total_bytes_processed", None)
    return BigQueryQueryResult(
        rows=rows,
        total_bytes_processed=int(total_bytes_processed) if total_bytes_processed is not None else None,
    )
