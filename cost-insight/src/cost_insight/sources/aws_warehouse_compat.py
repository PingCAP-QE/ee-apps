from __future__ import annotations

import re
from collections.abc import Iterable

_BIGQUERY_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_RESOURCE_TAG_COLUMN_RE = re.compile(r"^resource_tags_[A-Za-z0-9_]+$")


def build_warehouse_cur_relation(billing_table: str, tag_columns: Iterable[str] = ()) -> str:
    """Preserve CUR values with explicit physical bounds using existing DATE parameters."""
    if not _BIGQUERY_TABLE_RE.fullmatch(billing_table):
        raise ValueError(f"Invalid BigQuery table identifier: {billing_table!r}")
    tag_aliases = {
        "tag_used_by": "user_usedby",
        "tag_tenant": "user_tenant",
        "tag_project": "user_project",
        "tag_cluster": "user_cluster",
    }
    for column in tag_columns:
        if not _RESOURCE_TAG_COLUMN_RE.fullmatch(column):
            raise ValueError(f"Invalid CUR resource tag column: {column!r}")
        tag_aliases[column] = column.removeprefix("resource_tags_")
    tag_projections = ",\n  ".join(
        f"""(
    SELECT kv.value
    FROM UNNEST(raw.resource_tags.key_value) AS kv
    WHERE kv.key = '{key}'
    LIMIT 1
  ) AS {column}"""
        for column, key in tag_aliases.items()
    )
    return f"""(
  SELECT
    raw.*,
    FORMAT_DATE('%Y%m%d', DATE(raw.bill_billing_period_start_date)) AS billing_month,
  {tag_projections}
  FROM `{billing_table}` AS raw
  WHERE raw.bill_billing_period_start_date >= TIMESTAMP(@export_partition_start)
    AND raw.bill_billing_period_start_date < TIMESTAMP(DATE_ADD(@export_partition_end, INTERVAL 1 DAY))
)"""
