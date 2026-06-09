from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


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
