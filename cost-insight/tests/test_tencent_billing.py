import json
from datetime import date
from decimal import Decimal

import pytest

from cost_insight.sources.tencent_billing import (
    _default_client,
    expand_tencent_bill_details,
    fetch_tencent_bill_detail_page,
    fetch_tencent_bill_month_summary,
)


def _detail(**overrides):
    row = {
        "BillDay": "2026-09-13",
        "BillId": "bill-1",
        "OrderId": "order-1",
        "ResourceId": "eks-resource-1",
        "ResourceName": "pod-1",
        "FeeBeginTime": "2026-09-12 23:00:00",
        "FeeEndTime": "2026-09-13 00:00:00",
        "PayTime": "2026-09-14 08:20:52",
        "OwnerUin": "100050658403",
        "OperateUin": "100000000001",
        "BusinessCode": "p_eks",
        "BusinessCodeName": "TKE Serverless",
        "ProductCode": "sp_eks_supernode_intel_pod",
        "ProductCodeName": "Supernode Pod",
        "ActionType": "postpay",
        "ActionTypeName": "Pay as you go",
        "RegionId": "1",
        "RegionName": "Beijing",
        "Tags": [
            {"TagKey": "repo", "TagValue": "cost-insight"},
            {"TagKey": "author", "TagValue": "alice"},
            {"TagKey": "org", "TagValue": "pingcap-qe"},
            {"TagKey": "owner", "TagValue": "ee"},
        ],
        "ComponentSet": [
            {
                "ComponentCode": "cpu",
                "ComponentCodeName": "CPU",
                "ItemCode": "cpu-time",
                "ItemCodeName": "CPU time",
                "ComponentConfig": [
                    {"Name": "memory", "Value": "512Mi"},
                    {"Name": "cpu", "Value": "250m"},
                ],
                "Cost": "2.00000000",
                "RealCost": "1.50000000",
            },
            {
                "ComponentCode": "memory",
                "ComponentCodeName": "Memory",
                "ItemCode": "memory-time",
                "ItemCodeName": "Memory time",
                "ComponentConfig": [],
                "Cost": "1.00000000",
                "RealCost": "0.75000000",
            },
        ],
    }
    row.update(overrides)
    return row


def test_fetch_tencent_bill_detail_page_builds_exact_day_request() -> None:
    captured = {}

    class Response:
        def to_json_string(self):
            return json.dumps(
                {"DetailSet": [_detail()], "Total": 4456, "Context": "next-context"}
            )

    class Client:
        def DescribeBillDetailForOrganization(self, request):
            captured.update(json.loads(request.to_json_string()))
            return Response()

    page = fetch_tencent_bill_detail_page(
        bill_day=date(2026, 9, 13),
        offset=100,
        limit=100,
        context="previous-context",
        need_record_num=True,
        client=Client(),
    )

    assert captured["BeginTime"] == "2026-09-13 00:00:00"
    assert captured["EndTime"] == "2026-09-13 23:59:59"
    assert captured["Offset"] == 100
    assert captured["Limit"] == 100
    assert captured["Context"] == "previous-context"
    assert captured["NeedRecordNum"] == 1
    assert page.total == 4456
    assert page.context == "next-context"
    assert len(page.details) == 1


@pytest.mark.parametrize("limit", [0, 101])
def test_fetch_tencent_bill_detail_page_rejects_invalid_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        fetch_tencent_bill_detail_page(
            bill_day=date(2026, 9, 13), offset=0, limit=limit, client=object()
        )


def test_fetch_tencent_bill_month_summary_sums_organization_totals_with_decimal() -> None:
    captured = {}

    class Response:
        def to_json_string(self):
            return json.dumps(
                {
                    "Ready": 1,
                    "SummaryDetail": [
                        {
                            "Business": "p_eks",
                            "TotalCost": "200.00000001",
                            "RealTotalCost": "180.10000001",
                        },
                        {
                            "Business": "p_cvm",
                            "TotalCost": "0.10000000",
                            "RealTotalCost": "0.00000009",
                        },
                    ]
                }
            )

    class Client:
        def DescribeBillSummaryForOrganization(self, request):
            captured.update(json.loads(request.to_json_string()))
            return Response()

    summary = fetch_tencent_bill_month_summary(month="2026-08", client=Client())

    assert captured["Month"] == "2026-08"
    assert captured["GroupType"] == "business"
    assert summary.month == "2026-08"
    assert summary.total_cost == Decimal("200.10000001")
    assert summary.real_total_cost == Decimal("180.10000010")


def test_default_client_resolves_environment_credentials(monkeypatch) -> None:
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "test-id")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "test-key")

    client = _default_client()

    assert client.credential.get_credential_info() == ("test-id", "test-key", None)


def test_fetch_tencent_bill_month_summary_rejects_unready_response() -> None:
    class Response:
        def to_json_string(self):
            return json.dumps({"Ready": 0, "SummaryDetail": []})

    class Client:
        def DescribeBillSummaryForOrganization(self, _request):
            return Response()

    with pytest.raises(RuntimeError, match="not ready"):
        fetch_tencent_bill_month_summary(month="2026-08", client=Client())


def test_fetch_tencent_bill_month_summary_allows_unavailable_total_cost() -> None:
    class Response:
        def to_json_string(self):
            return json.dumps(
                {
                    "Ready": 1,
                    "SummaryDetail": [
                        {"TotalCost": "-", "RealTotalCost": "1.25"},
                        {"TotalCost": "2.00", "RealTotalCost": "0.75"},
                    ],
                }
            )

    class Client:
        def DescribeBillSummaryForOrganization(self, _request):
            return Response()

    summary = fetch_tencent_bill_month_summary(month="2026-08", client=Client())

    assert summary.total_cost is None
    assert summary.real_total_cost == Decimal("2.00")


def test_expand_tencent_components_maps_cny_tags_and_resource_identity() -> None:
    rows = expand_tencent_bill_details(
        [_detail()],
        expected_bill_day=date(2026, 9, 13),
        account_id="100050658403",
    )

    assert len(rows) == 2
    cpu = next(row for row in rows if "CPU time" in row["sku_name"])
    assert cpu["vendor"] == "tencent"
    assert cpu["account_id"] == "100050658403"
    assert cpu["billing_account_id"] is None
    assert cpu["export_partition_date"] == date(2026, 9, 13)
    assert cpu["usage_date"] == date(2026, 9, 12)
    assert cpu["resource_name"] == "eks-resource-1"
    assert cpu["author"] == "alice"
    assert cpu["org"] == "pingcap-qe"
    assert cpu["repo"] == "cost-insight"
    assert cpu["owner"] == "ee"
    assert json.loads(cpu["vendor_tags_json"])["repo"] == "cost-insight"
    assert json.loads(cpu["vendor_tags_json"])["__tencent_product_code"] == "sp_eks_supernode_intel_pod"
    assert cpu["list_cost"] == Decimal("2.00000000")
    assert cpu["effective_cost"] == Decimal("1.50000000")
    assert cpu["net_cost"] == Decimal("1.50000000")
    assert cpu["credit_amount"] is None
    assert cpu["currency"] == "CNY"


def test_service_label_falls_back_to_project_without_rewriting_source_tags() -> None:
    service_only = _detail(Tags=[{"TagKey": "service", "TagValue": "bazel"}])
    explicit_project = _detail(
        Tags=[
            {"TagKey": "service", "TagValue": "bazel"},
            {"TagKey": "project", "TagValue": "cache-platform"},
        ]
    )

    service_rows = expand_tencent_bill_details(
        [service_only], expected_bill_day=date(2026, 9, 13), account_id="100050658403"
    )
    project_rows = expand_tencent_bill_details(
        [explicit_project], expected_bill_day=date(2026, 9, 13), account_id="100050658403"
    )

    assert {(row["service"], row["project"]) for row in service_rows} == {("bazel", "bazel")}
    assert {(row["service"], row["project"]) for row in project_rows} == {
        ("bazel", "cache-platform")
    }
    assert json.loads(service_rows[0]["vendor_tags_json"]) == {
        "__tencent_product_code": "sp_eks_supernode_intel_pod",
        "service": "bazel",
    }


def test_tencent_identity_ignores_component_order_amounts_tags_and_settlement_times() -> None:
    original = _detail()
    changed = _detail(
        Tags=[{"TagKey": "author", "TagValue": "bob"}],
        FeeEndTime="2026-09-13 02:00:00",
        PayTime="2026-09-15 08:20:52",
        ComponentSet=list(reversed(_detail()["ComponentSet"])),
    )
    changed["ComponentSet"][0]["RealCost"] = "99.0"
    changed["ComponentSet"][1]["ComponentConfig"] = list(
        reversed(changed["ComponentSet"][1]["ComponentConfig"])
    )

    original_rows = expand_tencent_bill_details(
        [original], expected_bill_day=date(2026, 9, 13), account_id="100050658403"
    )
    changed_rows = expand_tencent_bill_details(
        [changed], expected_bill_day=date(2026, 9, 13), account_id="100050658403"
    )

    assert {row["source_row_hash"] for row in original_rows} == {
        row["source_row_hash"] for row in changed_rows
    }
    assert {row["author"] for row in changed_rows} == {"bob"}


def test_expand_tencent_bill_details_rejects_identity_and_scope_errors() -> None:
    with pytest.raises(ValueError, match="no components"):
        expand_tencent_bill_details(
            [_detail(ComponentSet=[])],
            expected_bill_day=date(2026, 9, 13),
            account_id="100050658403",
        )
    duplicate = _detail(ComponentSet=[_detail()["ComponentSet"][0]] * 2)
    with pytest.raises(ValueError, match="identity collision"):
        expand_tencent_bill_details(
            [duplicate], expected_bill_day=date(2026, 9, 13), account_id="100050658403"
        )
    with pytest.raises(ValueError, match="BillDay"):
        expand_tencent_bill_details(
            [_detail(BillDay="2026-09-12")],
            expected_bill_day=date(2026, 9, 13),
            account_id="100050658403",
        )
    with pytest.raises(ValueError, match="OwnerUin"):
        expand_tencent_bill_details(
            [_detail(OwnerUin="other")],
            expected_bill_day=date(2026, 9, 13),
            account_id="100050658403",
        )
