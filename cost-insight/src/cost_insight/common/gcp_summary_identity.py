"""Stable identity shared by GCP billing summary consumers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from cost_insight.common.row_utils import hash_value

GCP_SUMMARY_HASH_FIELDS = (
    "vendor",
    "account_id",
    "billing_account_id",
    "export_partition_date",
    "usage_date",
    "service_name",
    "sku_name",
    "region",
    "author",
    "org",
    "repo",
    "target_branch",
    "resource_name",
    "vendor_tags_json",
)
GCP_SPLIT_SUMMARY_HASH_FIELDS = GCP_SUMMARY_HASH_FIELDS + (
    "source_schema_version",
    "source_allocation_scope",
    "namespace",
    "workload_name",
    "workload_type",
    "owner",
    "service",
    "project",
    "service_exec_id",
)


def build_gcp_summary_row_hash(row: Mapping[str, Any]) -> str:
    """Return the identity used by ``cost_bq_export_summary_daily`` rows."""
    hash_fields = (
        GCP_SPLIT_SUMMARY_HASH_FIELDS
        if row.get("is_split_source")
        else GCP_SUMMARY_HASH_FIELDS
    )
    if row.get("vendor_tags_json") is None:
        hash_fields = tuple(field for field in hash_fields if field != "vendor_tags_json")
    payload = {field: hash_value(row.get(field)) for field in hash_fields}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
