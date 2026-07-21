from __future__ import annotations

from collections.abc import Mapping
from typing import Any


COST_DRIVER_LABELS = {
    "compute": "Compute",
    "block_storage": "Block storage",
    "nat": "NAT",
    "data_transfer": "Data transfer",
    "object_storage": "Object storage",
    "logs": "Logs",
    "other": "Other",
}


def classify_cost_driver(row: Mapping[str, Any]) -> str:
    service = _text(row.get("service_name"))
    sku = _text(row.get("sku_name"))
    usage = _text(row.get("usage_type"))
    text = f"{service} {sku} {usage}"

    if _has(text, "cloudwatch", "cloudtrail", "cloud logging", "logging"):
        return "logs"
    if _has(text, "natgateway", "nat gateway", "cloud nat"):
        return "nat"
    if _has(text, "data transfer", "datatransfer", "egress", "inter-region", "inter zone"):
        return "data_transfer"
    if _has(
        text,
        "ebs",
        "volumeusage",
        "volume usage",
        "snapshot",
        "iops",
        "throughput",
        "persistent disk",
        "hyperdisk",
    ):
        return "block_storage"
    if _has(text, "amazons3", "amazon s3", "cloud storage", "timedstorage"):
        return "object_storage"
    if _has(text, "amazonec2", "compute engine", "boxusage", "instance"):
        return "compute"
    return "other"


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _has(value: str, *needles: str) -> bool:
    return any(needle in value for needle in needles)
