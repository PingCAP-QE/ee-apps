from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from cost_insight.common.cost_drivers import classify_cost_driver


@dataclass(frozen=True)
class TencentBillPage:
    details: tuple[dict[str, Any], ...]
    total: int | None
    context: str | None


@dataclass(frozen=True)
class TencentBillMonthSummary:
    month: str
    total_cost: Decimal | None
    real_total_cost: Decimal


class TencentBillSummaryNotReady(RuntimeError):
    pass


def _default_client() -> Any:
    from tencentcloud.billing.v20180709.billing_client import BillingClient
    from tencentcloud.common.credential import EnvironmentVariableCredential

    credential = EnvironmentVariableCredential().get_credential()
    if credential is None:
        raise ValueError(
            "TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY are required"
        )
    return BillingClient(credential, "")


def fetch_tencent_bill_month_summary(
    *,
    month: str,
    client: Any | None = None,
) -> TencentBillMonthSummary:
    """Read one closed month's organization totals via DescribeBillSummaryForOrganization.

    The request takes a month (YYYY-MM) and a group type; a single business-grouped
    request returns the organization's monthly summary rows. Sum the rows with Decimal:
    the caller reconciles these totals against the imported detail facts.
    """
    if client is None:
        client = _default_client()

    from tencentcloud.billing.v20180709 import models

    request = models.DescribeBillSummaryForOrganizationRequest()
    request.Month = month
    request.GroupType = "business"

    response = client.DescribeBillSummaryForOrganization(request)
    payload = json.loads(response.to_json_string())
    if int(payload.get("Ready") or 0) != 1:
        raise TencentBillSummaryNotReady(f"Tencent bill summary for {month} is not ready")

    total_cost: Decimal | None = Decimal(0)
    real_total_cost = Decimal(0)
    for item in payload.get("SummaryDetail") or ():
        item_total = _optional_decimal(item.get("TotalCost"))
        if item_total is None:
            total_cost = None
        elif total_cost is not None:
            total_cost += item_total
        real_total_cost += _decimal(item.get("RealTotalCost"))
    return TencentBillMonthSummary(
        month=month,
        total_cost=total_cost,
        real_total_cost=real_total_cost,
    )


def fetch_tencent_bill_detail_page(
    *,
    bill_day: date,
    offset: int,
    limit: int,
    context: str | None = None,
    need_record_num: bool = False,
    client: Any | None = None,
) -> TencentBillPage:
    if not 1 <= limit <= 100:
        raise ValueError(f"Tencent billing page limit must be between 1 and 100, got {limit}")
    if offset < 0:
        raise ValueError(f"Tencent billing offset must be non-negative, got {offset}")

    if client is None:
        client = _default_client()

    from tencentcloud.billing.v20180709 import models

    request = models.DescribeBillDetailForOrganizationRequest()
    request.BeginTime = f"{bill_day.isoformat()} 00:00:00"
    request.EndTime = f"{bill_day.isoformat()} 23:59:59"
    request.Offset = offset
    request.Limit = limit
    request.NeedRecordNum = 1 if need_record_num else 0
    if context:
        request.Context = context

    response = client.DescribeBillDetailForOrganization(request)
    payload = json.loads(response.to_json_string())
    return TencentBillPage(
        details=tuple(payload.get("DetailSet") or ()),
        total=int(payload["Total"]) if payload.get("Total") is not None else None,
        context=str(payload["Context"]) if payload.get("Context") else None,
    )


def expand_tencent_bill_details(
    details: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    expected_bill_day: date,
    account_id: str,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    page_hashes: set[str] = set()
    for detail in details:
        bill_day = _bill_day(detail)
        if bill_day != expected_bill_day:
            raise ValueError(
                f"Tencent billing row BillDay {bill_day!s} does not match {expected_bill_day}"
            )
        owner_uin = _required_text(detail, "OwnerUin")
        if owner_uin != account_id:
            raise ValueError(
                f"Tencent billing row OwnerUin {owner_uin!r} does not match {account_id!r}"
            )

        components = detail.get("ComponentSet") or ()
        if not components:
            raise ValueError(
                f"Tencent billing row has no components: BillId={detail.get('BillId')!r}"
            )
        component_keys: set[tuple[str, str, str]] = set()
        for component in components:
            component_config = _canonical_component_config(component.get("ComponentConfig"))
            component_key = (
                str(component.get("ComponentCode") or ""),
                str(component.get("ItemCode") or ""),
                component_config,
            )
            if component_key in component_keys:
                raise ValueError(
                    "Tencent billing component identity collision within outer row: "
                    f"BillId={detail.get('BillId')!r}, key={component_key!r}"
                )
            component_keys.add(component_key)

            source_row_hash = _source_row_hash(detail, component, component_config)
            if source_row_hash in page_hashes:
                raise ValueError(
                    f"Tencent billing source identity collision within page: {source_row_hash}"
                )
            page_hashes.add(source_row_hash)
            rows.append(
                _summary_row(
                    detail,
                    component,
                    component_config=component_config,
                    source_row_hash=source_row_hash,
                    account_id=account_id,
                )
            )
    return tuple(rows)


def _summary_row(
    detail: dict[str, Any],
    component: dict[str, Any],
    *,
    component_config: str,
    source_row_hash: str,
    account_id: str,
) -> dict[str, Any]:
    tags = _canonical_tags(detail.get("Tags") or [])
    usage_date = _parse_datetime(_required_text(detail, "FeeBeginTime")).date()
    service_name = _first_text(detail, "BusinessCodeName", "BusinessCode")
    sku_name = " / ".join(
        value
        for value in (
            _first_text(detail, "ProductCodeName", "ProductCode"),
            _first_text(component, "ComponentCodeName", "ComponentCode"),
            _first_text(component, "ItemCodeName", "ItemCode"),
        )
        if value
    ) or None
    row = {
        "vendor": "tencent",
        "account_id": account_id,
        "billing_account_id": None,
        "export_partition_date": _bill_day(detail),
        "usage_date": usage_date,
        "service_name": service_name,
        "sku_name": sku_name,
        "usage_type": _first_text(detail, "ActionTypeName", "ActionType"),
        "region": _first_text(detail, "RegionName", "RegionId"),
        "org": tags.get("org"),
        "repo": tags.get("repo"),
        "target_branch": tags.get("target_branch"),
        "resource_name": _first_text(detail, "ResourceId", "ResourceName"),
        "vendor_tags_json": _canonical_json(tags) if tags else None,
        "author": tags.get("author"),
        "source_schema_version": None,
        "source_allocation_scope": "direct",
        "cluster_name": None,
        "cluster_location": None,
        "kubernetes_cost_class": None,
        "kubernetes_residual_type": None,
        "kubernetes_cost_component": None,
        "namespace": None,
        "workload_name": None,
        "workload_type": None,
        "owner": tags.get("owner"),
        "service": tags.get("service"),
        "project": tags.get("project"),
        "service_exec_id": tags.get("service_exec_id"),
        "list_cost": _decimal(component.get("Cost")),
        "effective_cost": _decimal(component.get("RealCost")),
        "credit_amount": None,
        "net_cost": _decimal(component.get("RealCost")),
        "currency": "CNY",
        "source_export_time": _parse_optional_datetime(detail.get("PayTime")),
        "source_row_hash": source_row_hash,
    }
    row["cost_driver_key"] = classify_cost_driver(row)
    return row


def _source_row_hash(
    detail: dict[str, Any],
    component: dict[str, Any],
    component_config: str,
) -> str:
    payload = [
        _bill_day(detail).isoformat(),
        detail.get("BillId"),
        detail.get("OrderId"),
        detail.get("ResourceId"),
        detail.get("FeeBeginTime"),
        detail.get("OwnerUin"),
        detail.get("OperateUin"),
        detail.get("BusinessCode"),
        detail.get("ProductCode"),
        detail.get("ActionType"),
        component.get("ComponentCode"),
        component.get("ItemCode"),
        component_config,
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_component_config(value: Any) -> str:
    if not value:
        return "[]"
    if not isinstance(value, list):
        raise ValueError(f"Tencent ComponentConfig must be a list, got {type(value).__name__}")
    return _canonical_json(sorted(value, key=_canonical_json))


def _canonical_tags(tags: Any) -> dict[str, str]:
    if not isinstance(tags, list):
        raise ValueError(f"Tencent Tags must be a list, got {type(tags).__name__}")
    result: dict[str, str] = {}
    for tag in tags:
        key = str(tag.get("TagKey") or "").strip()
        if not key:
            continue
        value = str(tag.get("TagValue") or "")
        if key in result and result[key] != value:
            raise ValueError(f"Tencent billing row contains conflicting values for tag {key!r}")
        result[key] = value
    return dict(sorted(result.items()))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bill_day(row: dict[str, Any]) -> date:
    return date.fromisoformat(_required_text(row, "BillDay")[:10])


def _required_text(row: dict[str, Any], key: str) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise ValueError(f"Tencent billing row is missing {key}")
    return value


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value if value not in (None, "") else 0))


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value in (None, "", "-") else Decimal(str(value))


def _parse_optional_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    return _parse_datetime(text) if text else None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
