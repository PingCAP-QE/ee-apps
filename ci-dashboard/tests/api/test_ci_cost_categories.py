from collections import defaultdict
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from ci_dashboard.api.queries.ci_cost_categories import get_ci_weekly_cost_category_share
from ci_dashboard.api.queries.cost import _billing_report_list_cost_expr, _usd_cost_expr

START = date(2026, 10, 5)
END = date(2026, 10, 11)
ACCOUNTS = {"gcp": "pingcap-testing-account", "tencent": "100050658403"}


def _insert(engine, vendor="gcp", **overrides):
    values = {
        "vendor": vendor,
        "account_id": ACCOUNTS[vendor],
        "usage_date": "2026-10-05",
        "service_name": "unknown",
        "sku_name": "runner",
        "resource_name": None,
        "source_allocation_scope": "direct",
        "list_cost": 1,
        "net_cost": 1,
        "currency": "USD",
        "attribution_source": "billing",
        "attribution_status": "unmatched",
        "dimension_hash": uuid4().hex,
        **overrides,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO cost_attribution_daily ({', '.join(values)}) "
                f"VALUES ({', '.join(f':{key}' for key in values)})"
            ),
            values,
        )


def _report(engine):
    with engine.connect() as connection:
        return get_ci_weekly_cost_category_share(
            connection,
            START,
            END,
            list_cost_expr=_billing_report_list_cost_expr("c"),
            net_cost_expr=_usd_cost_expr("c", "c.net_cost"),
        )


def test_category_rules_are_mutually_exclusive_and_keep_unknown_resources(sqlite_engine):
    cases = [
        ("gcp", "Compute Engine", "pvc-node", "gke_direct", "gke"),
        ("gcp", "Artifact Registry", None, "gke_residual", "gke"),
        ("gcp", "Compute Engine", "pvc-volume", "direct", "gcp_pvc"),
        ("gcp", "Compute Engine", "projects/x/zones/y/disks/pvc-volume", "direct", "gcp_pvc"),
        ("gcp", "Artifact Registry", None, "direct", "artifact_registry"),
        ("gcp", "Cloud Storage", "pingcap-ci-bazel-remote-cache-us-central1", "direct", "gcp_bazel"),
        ("gcp", "Cloud Storage", "pingcap-ci-bazel-mirror-static-us-central1", "direct", "gcp_bazel"),
        ("gcp", "Cloud Storage", "prow-job-cache", "direct", "prow_cache"),
        ("gcp", "Cloud Storage", "other", "direct", "object_storage"),
        ("gcp", "Cloud Logging", None, "direct", "logging"),
        ("gcp", "BigQuery", None, "direct", "bigquery"),
        ("gcp", "Networking", None, "direct", "networking"),
        ("gcp", "Compute Engine", "projects/x/instances/OpenClaw-test", "direct", "openclaw"),
        ("gcp", "Compute Engine", "unknown", "direct", "other"),
        ("gcp", None, "prow-job-cache", "direct", "other"),
        ("tencent", "容器服务 TKE", "node", "direct", "tke"),
        ("tencent", "Serverless 容器服务", "eks-node", "direct", "tke"),
        ("tencent", "云硬盘CBS", "disk-id", "direct", "cbs"),
        ("tencent", "COS 对象存储", "ee-brc-1453452848", "direct", "tencent_bazel"),
        ("tencent", "COS 对象存储", "ee-jenkins-cache-1453452848", "direct", "jenkins_cache"),
        ("tencent", "COS 对象存储", "ee-jenkins-artifacts-1453452848", "direct", "jenkins_artifacts"),
        ("tencent", "COS 对象存储", "ee-zot-1453452848", "direct", "zot"),
        ("tencent", "COS 对象存储", "new-bucket", "direct", "object_storage"),
        ("tencent", "NAT网关", None, "direct", "networking"),
        ("tencent", "负载均衡", None, "direct", "networking"),
        ("tencent", "公网 IP", None, "direct", "networking"),
        ("tencent", "全球应用加速GAAP", None, "direct", "networking"),
        ("tencent", "云服务器CVM", None, "direct", "cvm"),
        ("tencent", "私有域解析 Private DNS", None, "direct", "dns_logs"),
        ("tencent", "日志服务CLS", None, "direct", "dns_logs"),
        ("tencent", "专线接入", None, "direct", "other"),
    ]
    expected = {"gcp": defaultdict(float), "tencent": defaultdict(float)}
    for vendor, service, resource, scope, key in cases:
        _insert(sqlite_engine, vendor, service_name=service, resource_name=resource,
                source_allocation_scope=scope)
        expected[vendor][key] += 1
    accounts = _report(sqlite_engine)["accounts"]
    for account in accounts:
        assert {item["key"]: item["value"] for item in account["items"]} == expected[account["vendor"]]
        assert sum(item["value"] for item in account["items"]) == account["total_cost"]
        assert all(item["evidence"] for item in account["items"])
        assert len({item["key"] for item in account["items"]}) == len(account["items"])
    gcp = {item["key"]: item for item in accounts[0]["items"]}
    assert "Jenkins" not in gcp["prow_cache"]["name"]
    assert "not self-hosted" in gcp["artifact_registry"]["evidence"]
    tencent = {item["key"]: item for item in accounts[1]["items"]}
    assert "storage only" in tencent["zot"]["evidence"]


def test_legacy_cos_rows_use_ownership_without_inventing_bucket_detail(sqlite_engine):
    for label in ("bazel", "jenkins", "zot"):
        _insert(sqlite_engine, "tencent", service_name="COS 对象存储", service=label,
                project=label, resource_name=None)
        _insert(sqlite_engine, "tencent", service_name="COS 对象存储", service=None,
                project=label, resource_name=None)
    _insert(sqlite_engine, "tencent", service_name="COS 对象存储", service="jenkins", project="bazel")
    _insert(sqlite_engine, "tencent", service_name="COS 对象存储", resource_name="unknown-bucket",
            service="bazel", project="bazel")
    tencent = _report(sqlite_engine)["accounts"][1]
    assert {item["key"]: item["value"] for item in tencent["items"]} == {
        "tencent_bazel": 2, "jenkins_storage": 3, "zot": 2, "object_storage": 1,
    }
    jenkins = next(item for item in tencent["items"] if item["key"] == "jenkins_storage")
    assert "cannot be split" in jenkins["evidence"]


def test_category_costs_use_mixed_usd_basis_and_keep_small_negative_adjustments(sqlite_engine):
    _insert(sqlite_engine, service_name="Cloud Logging", list_cost=13, net_cost=1)
    _insert(sqlite_engine, list_cost=999, net_cost=50,
            sku_name="Compute Flexible Committed Use Discounts v1", usage_date="2026-10-06")
    _insert(sqlite_engine, list_cost=None, net_cost=999, usage_date="2026-10-07")
    _insert(sqlite_engine, list_cost=-0.01, net_cost=-0.5)
    _insert(sqlite_engine, "tencent", service_name="容器服务 TKE", currency="CNY",
            list_cost=650, net_cost=65)
    _insert(sqlite_engine, "tencent", service_name="云硬盘CBS", currency="CNY",
            list_cost=1300, net_cost=32.5)
    _insert(sqlite_engine, "tencent", service_name="COS 对象存储", list_cost=0, net_cost=2.001,
            resource_name="ee-brc-1453452848")
    _insert(sqlite_engine, "tencent", list_cost=9, net_cost=-0.0001)
    gcp, tencent = _report(sqlite_engine)["accounts"]
    assert gcp["cost_basis"] == "list_cost"
    assert gcp["total_cost"] == pytest.approx(12.99)
    assert {item["key"]: item["value"] for item in gcp["items"]} == {"logging": 13, "other": -0.01}
    assert gcp["observed_dates"] == ["2026-10-05", "2026-10-06", "2026-10-07"]
    assert tencent["cost_basis"] == "net_cost"
    assert tencent["total_cost"] == pytest.approx(17.0009)
    assert {item["key"]: item["value"] for item in tencent["items"]} == {
        "tke": 10, "cbs": 5, "tencent_bazel": 2.001, "other": -0.0001,
    }


def test_category_query_is_one_bounded_aggregation_with_date_coverage(sqlite_engine):
    _insert(sqlite_engine, usage_date="2026-10-05", list_cost=2)
    _insert(sqlite_engine, usage_date="2026-10-11", list_cost=3)
    _insert(sqlite_engine, usage_date="2026-10-04", list_cost=100)
    _insert(sqlite_engine, usage_date="2026-10-12", list_cost=100)
    _insert(sqlite_engine, account_id="other-account", list_cost=100)
    _insert(sqlite_engine, account_id=ACCOUNTS["tencent"], list_cost=100)
    _insert(sqlite_engine, "tencent", account_id=ACCOUNTS["gcp"], net_cost=100)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(sqlite_engine, "before_cursor_execute", capture)
    try:
        report = _report(sqlite_engine)
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert "GROUP BY" in statements[0]
    assert "BETWEEN" in statements[0]
    assert report["period"] == {"start_date": "2026-10-05", "end_date": "2026-10-11"}
    gcp, tencent = report["accounts"]
    assert gcp["total_cost"] == 5
    assert gcp["observed_dates"] == ["2026-10-05", "2026-10-11"]
    assert gcp["missing_dates"] == [f"2026-10-{day:02d}" for day in range(6, 11)]
    assert tencent["total_cost"] == 0
    assert tencent["items"] == []
    assert len(tencent["missing_dates"]) == 7


def test_category_shares_retain_precision_at_the_one_percent_boundary(sqlite_engine):
    for service, value in [("Cloud Logging", 97.8999), ("BigQuery", 1.0001),
                           ("Networking", 1), ("unknown", 0.1)]:
        _insert(sqlite_engine, service_name=service, list_cost=value)
    gcp = _report(sqlite_engine)["accounts"][0]
    assert gcp["total_cost"] == 100
    items = {item["key"]: item for item in gcp["items"]}
    assert items["bigquery"]["value"] > gcp["total_cost"] / 100
    assert items["bigquery"]["share_pct"] == 1.0001
    assert items["networking"]["share_pct"] == 1
    assert items["other"]["value"] == 0.1


def test_category_share_returns_both_empty_accounts(sqlite_engine):
    gcp, tencent = _report(sqlite_engine)["accounts"]
    assert gcp["cost_source"] == "gcp:pingcap-testing-account"
    assert tencent["cost_source"] == "tencent:100050658403"
    for account in (gcp, tencent):
        assert account["total_cost"] == 0
        assert account["items"] == []
        assert account["observed_dates"] == []
        assert len(account["missing_dates"]) == 7
