"""Offline V2 source tests: all SDK/cloud calls mocked, no runner or database imports."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from cost_insight.utilization import tencent

EXAMPLE = Path(__file__).parents[1] / "config/tencent-utilization.example.json"
DAY = date(2026, 11, 20)
START = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=1)
END = START + timedelta(minutes=10)


@pytest.fixture
def profile(tmp_path):
    raw = json.loads(EXAMPLE.read_text())
    raw["measurement"].update(
        verified_horizon_seconds=7 * 86400,
        horizon_verified_at=START.isoformat(),
        evidence_reference="offline fixture only, NOT production verification",
        timestamp_alignment_verified=True,
        source_scales={
            "cvm": {"cpu": 1, "memory": 1},
            "tke_native": {"cpu": 1, "memory": 1},
            "pod": {"cpu": 1, "memory": 1_000_000},
        },
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw))
    return tencent.load_profile(str(path))


def repin(profile):
    profile["contract_version"] = tencent.contract_version(profile)


def target(kind="cvm", **overrides):
    result = {
        "kind": "machine",
        "machine_id": "ins-one" if kind == "cvm" else "np-one",
        "resource_class": kind,
        "region": "ap-beijing",
        "cluster_id": "" if kind == "cvm" else "cls-9zpon9y1",
    }
    if kind == "pod":
        result = {
            "kind": "pod",
            "pod_uid": "uid-one",
            "pod_name": "build-abc",
            "namespace": "jenkins-tidb",
            "region": "ap-beijing",
            "cluster_id": "cls-9zpon9y1",
            "node_query_kind": "node",
            "node_query_value": "10.0.6.207",
        }
    return {**result, **overrides}


def metric_metadata(namespace):
    return {
        "MetricSet": [
            {
                "MetricName": name,
                "Unit": "%" if kind != "pod" else "None" if metric == "cpu" else "MBytes",
                "Periods": [{"Period": "60", "StatType": ["avg"]}],
            }
            for kind, metrics in tencent.ALLOWED_METRICS.items()
            if (kind == "cvm") == (namespace == "QCE/CVM")
            for metric, name in metrics.items()
        ]
    }


def cvm_response(entity="ins-one", value=50):
    return {
        "Period": 60,
        "DataPoints": [
            {
                "Dimensions": [{"Name": "InstanceId", "Value": entity}],
                "Timestamps": [START.timestamp(), END.timestamp()],
                "Values": [],
                "AvgValues": [value, 99],
            }
        ],
    }


def tke_response(kind="pod", value=2, **overrides):
    snap = target(kind)
    dims = {"tke_cluster_instance_id": snap["cluster_id"]}
    if kind == "pod":
        dims.update(namespace=snap["namespace"], pod_name=snap["pod_name"])
        dims[snap["node_query_kind"]] = snap["node_query_value"]
    else:
        dims["un_instance_id"] = snap["machine_id"]
    dims.update(overrides)
    return {
        "Period": 60,
        "Data": [
            {
                "MetricName": name,
                "Points": [
                    {
                        "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                        "Values": [
                            {"Timestamp": START.timestamp(), "Value": value},
                            {
                                "Timestamp": (START + timedelta(minutes=1)).timestamp(),
                                "Value": None,
                            },
                            {"Timestamp": END.timestamp(), "Value": 999},
                        ],
                    }
                ],
            }
            for name in tencent.ALLOWED_METRICS[kind].values()
        ],
    }


def component(code="sv_011691_nn_sa9_cpu", amount="8", unit="核", config=None):
    return {
        "ItemCode": code,
        "UsedAmount": amount,
        "UsedAmountUnit": unit,
        "ComponentConfig": config or [],
    }


def bill(product="p_cvm", **overrides):
    configs = [{"Name": "地域", "Value": "ap-beijing"}, {"Name": "机型", "Value": "SA9.MEDIUM4"}]
    components = (
        [component("cvm-instance", "1", "个", configs)]
        if product == "p_cvm"
        else [
            component(),
            component("sv_011691_nn_sa9_cpu_servicefee"),
            component("sv_011691_nn_sa9_mem", "32", "GiB"),
            component("sv_011691_nn_sa9_mem_servicefee", "32", "GiB"),
        ]
    )
    return dict(
        OwnerUin=tencent.ACCOUNT_ID,
        BusinessCode=product,
        ResourceId="ins-one" if product == "p_cvm" else "cls-9zpon9y1_np-one",
        RegionId="8",
        BillDay="2026-11-25",  # Not the usage day; must not be used as identity.
        FeeBeginTime="2026-11-20 11:36:36",
        FeeEndTime="2026-11-20 12:26:24",
        ComponentSet=components,
        **overrides,
    )


def api_call(service, action, region, params):
    if action == "GetCallerIdentity":
        return {"Response": {"AccountId": tencent.ACCOUNT_ID}}
    if action == "DescribeBaseMetrics":
        return metric_metadata(params["Namespace"])
    if action == "DescribeBillDetailForOrganization":
        return {"DetailSet": [bill(params["BusinessCode"])], "Total": 1}
    if action == "DescribeInstanceTypeConfigs":
        return {"InstanceTypeConfigSet": [{"InstanceType": "SA9.MEDIUM4", "CPU": 2, "Memory": 4}]}
    if action == "GetMonitorData":
        return cvm_response()
    if action == "DescribeStatisticData":
        kind = "pod" if params["MetricNames"][0].startswith("K8sPod") else "tke_native"
        return tke_response(kind)
    raise AssertionError(f"Unexpected offline action: {action}")


def reader(profile, call=api_call, sleep=None):
    return tencent.TencentReader(profile, call=call, sleep=sleep or Mock())


def test_profile_is_v2_scoped_stable_and_example_is_unverified(tmp_path, profile):
    assert set(profile["contracts"]) == {"cvm", "tke_native", "pod"}
    assert all(set(metrics) == {"cpu", "memory"} for metrics in profile["contracts"].values())
    assert "scope_contracts" not in str(profile) and "allocatable" not in str(profile)
    assert tencent.contract_version(profile) == profile["contract_version"]
    with pytest.raises(ValueError):
        tencent.load_profile(str(EXAMPLE))
    unverified = tencent.load_profile(str(EXAMPLE), probe=True)
    assert unverified["contracts"]["pod"]["memory"]["contract"]["source_scale"] == 0
    changed = json.loads(EXAMPLE.read_text())
    changed["labels"].remove("repo")
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(changed))
    assert (
        tencent.load_profile(str(path), probe=True)["contract_version"]
        != unverified["contract_version"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "other"),
        ("regions", []),
        ("regions", ["bad"]),
        ("regions", [True]),
        ("cluster_id", "bad"),
        ("cluster_region", "ap-other"),
        ("namespaces", []),
        ("namespaces", [""]),
        ("namespaces", ["x", "x"]),
        ("labels", ["author"]),
        ("labels", ["owner"]),
    ],
)
def test_invalid_profile_scope(profile, field, value):
    profile[field] = value
    with pytest.raises(ValueError):
        tencent.validate_profile(profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_scale", 0),
        ("source_scale", float("nan")),
        ("source_scale", True),
        ("source_scale", 100),
        ("metric", "DiskUsage"),
        ("source_stat", "max"),
        ("period", 300),
        ("denominator", "allocatable"),
        ("normalized_unit", "%"),
        ("semantic_version", "v1"),
        ("source_scope_hash", "wrong"),
    ],
)
def test_invalid_pinned_contract(profile, field, value):
    profile["contracts"]["cvm"]["cpu"]["contract"][field] = value
    repin(profile)
    with pytest.raises(ValueError):
        tencent.validate_profile(profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("verified_horizon_seconds", 0),
        ("verified_horizon_seconds", True),
        ("safety_margin_seconds", 7 * 86400),
        ("timestamp_alignment_verified", False),
        ("horizon_verified_at", None),
        ("evidence_reference", None),
        ("horizon_verified_at", "2026-01-01T00:00:00"),
    ],
)
def test_unverified_evidence_rejected(profile, field, value):
    profile["contracts"]["pod"]["cpu"][field] = value
    repin(profile)
    with pytest.raises(ValueError):
        tencent.validate_profile(profile)


def test_contract_version_tampering_and_v1_rejected(profile):
    profile["contracts"]["cvm"]["cpu"]["evidence_reference"] = "changed"
    with pytest.raises(ValueError, match="version"):
        tencent.validate_profile(profile)
    repin(profile)
    profile["contracts"]["pod"]["cpu"]["scope_contracts"] = {}
    repin(profile)
    with pytest.raises(ValueError, match="V1"):
        tencent.validate_profile(profile)


def test_historical_billing_not_live_inventory_and_service_fee_not_additive(profile):
    call = Mock(side_effect=api_call)
    source = reader(profile, call)
    rows = source.billing_candidates(DAY)
    assert len(rows) == 2
    cvm, native = rows
    assert cvm["cpu_cores"] == 2 and cvm["memory_bytes"] == 4 * 1024**3
    assert native["cpu_cores"] == 8 and native["memory_bytes"] == 32 * 1024**3
    assert native["machine_id"] == "np-one" and native["resource_class"] == "tke_native"
    assert native["instance_type"] == ""  # Component family is not an instance SKU.
    assert cvm["observed_from"] == datetime(2026, 11, 20, 3, 36, 36, tzinfo=UTC).replace(
        tzinfo=None
    )
    assert cvm["observed_until"] == datetime(2026, 11, 20, 4, 26, 24, tzinfo=UTC).replace(
        tzinfo=None
    )
    assert {c.args[1] for c in call.call_args_list} == {
        "GetCallerIdentity",
        "DescribeBillDetailForOrganization",
        "DescribeInstanceTypeConfigs",
    }
    requests = [c.args[3] for c in call.call_args_list if c.args[0] == "billing"]
    assert {r["BusinessCode"] for r in requests} == {"p_cvm", "p_011691"}
    assert all(r["Limit"] == 100 and r["NeedRecordNum"] == 1 for r in requests)


@pytest.mark.parametrize("product,prefix", [("p_cvm", "ins"), ("p_011691", "np")])
@pytest.mark.parametrize("actual,cached_total", [(1, 0), (100, 100), (101, 100)])
def test_billing_requires_short_page_even_after_cached_total(
    profile, product, prefix, actual, cached_total
):
    machine_ids = [f"{prefix}-{i}" for i in range(actual)]
    resource_prefix = "" if product == "p_cvm" else "cls-9zpon9y1_"
    rows = [{**bill(product), "ResourceId": resource_prefix + name} for name in machine_ids]
    offsets = []

    def call(service, action, region, params):
        if service != "billing":
            return api_call(service, action, region, params)
        if params["BusinessCode"] != product:
            return {"DetailSet": [], "Total": 0}
        offset = params["Offset"]
        offsets.append(offset)
        assert params["NeedRecordNum"] == int(offset == 0)
        page = {"DetailSet": rows[offset : offset + params["Limit"]]}
        if offset == 0:
            page["Total"] = cached_total
        return page

    snapshots = reader(profile, call).billing_candidates(DAY)
    assert {s["machine_id"] for s in snapshots} == set(machine_ids)
    assert offsets == list(range(0, actual + 1, 100))


def test_billing_complete_context_pagination_usage_filter_and_capacity_cache(profile):
    calls = []

    def call(service, action, region, params):
        if action != "DescribeBillDetailForOrganization":
            return api_call(service, action, region, params)
        calls.append(params)
        if params["BusinessCode"] == "p_011691":
            return {"DetailSet": [], "Total": 0}
        if params["Offset"] == 0:
            outside = bill()
            outside.update(FeeBeginTime="2026-11-19 12:00:00", FeeEndTime="2026-11-19 13:00:00")
            return {"DetailSet": [outside] * 99 + [bill()], "Total": 101, "Context": "opaque-context"}
        assert params["Offset"] == 100 and params["Context"] == "opaque-context"
        return {"DetailSet": [{**bill(), "ResourceId": "ins-two"}]}

    mock = Mock(side_effect=call)
    snapshots = reader(profile, mock).billing_candidates(DAY)
    assert {s["machine_id"] for s in snapshots} == {"ins-one", "ins-two"}
    assert len(calls) == 3
    assert [c.args[1] for c in mock.call_args_list].count("DescribeInstanceTypeConfigs") == 1


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda r: r.update(OwnerUin="other"), "OwnerUin"),
        (lambda r: r.update(BusinessCode="p_tke"), "product"),
        (lambda r: r.update(RegionId="999", ComponentSet=[component()]), "region"),
        (lambda r: r.update(FeeEndTime=r["FeeBeginTime"]), "interval"),
        (lambda r: r.update(ResourceId="bad"), "identity"),
        (lambda r: r["ComponentSet"][0].update(UsedAmount="9"), "Conflicting"),
        (lambda r: r["ComponentSet"][0].update(UsedAmountUnit="MHz"), "unit"),
        (lambda r: r["ComponentSet"][0].update(UsedAmount=True), "quantity"),
        (lambda r: r["ComponentSet"][0].update(UsedAmount="nan"), "finite"),
        (lambda r: r["ComponentSet"][0].update(ItemCode="sv_011691_unknown_cpu"), "Unreviewed"),
    ],
)
def test_bad_native_billing_fails_closed(profile, mutate, match):
    row = bill("p_011691")
    mutate(row)

    def call(service, action, region, params):
        if action == "DescribeBillDetailForOrganization" and params["BusinessCode"] == "p_011691":
            return {"DetailSet": [row], "Total": 1}
        return api_call(service, action, region, params)

    with pytest.raises((TypeError, ValueError), match=match):
        reader(profile, call).billing_candidates(DAY)


@pytest.mark.parametrize(
    "page",
    [
        {"DetailSet": [], "Total": 1},
        {"DetailSet": [bill()], "Total": 2},
        {"DetailSet": [], "Total": tencent.BILLING_ROW_LIMIT + 1},
        {"DetailSet": [], "Total": True},
        {"DetailSet": []},
        {"DetailSet": None, "Total": 0},
    ],
)
def test_incomplete_over_budget_billing_never_returns_partial(profile, page):
    def call(service, action, region, params):
        if action == "DescribeBillDetailForOrganization":
            return page
        return api_call(service, action, region, params)

    with pytest.raises(ValueError):
        reader(profile, call).billing_candidates(DAY)


@pytest.mark.parametrize("scenario", ["repeated", "budget", "premature_eof"])
def test_billing_full_page_does_not_prove_completion(profile, monkeypatch, scenario):
    # CVM succeeds first, but the TKE-native scan must not yield partial results.
    rows = [{**bill("p_011691"), "ResourceId": f"cls-9zpon9y1_np-{i}"} for i in range(100)]
    offsets = []
    if scenario == "budget":
        monkeypatch.setattr(tencent, "BILLING_ROW_LIMIT", 100)

    def call(service, action, region, params):
        if service != "billing" or params["BusinessCode"] != "p_011691":
            return api_call(service, action, region, params)
        offset = params["Offset"]
        offsets.append(offset)
        if offset == 0:
            return {"DetailSet": rows, "Total": 101 if scenario == "premature_eof" else 100}
        assert offset == 100
        return {"DetailSet": rows if scenario == "repeated" else []}

    match = {"repeated": "Repeated", "budget": "over budget", "premature_eof": "incomplete"}
    with pytest.raises(ValueError, match=match[scenario]):
        reader(profile, call).billing_candidates(DAY)
    assert offsets == ([0] if scenario == "budget" else [0, 100])


def test_billing_scan_has_headroom_above_ten_thousand_and_reads_the_tail(profile):
    total = 10_001
    offsets = []

    def call(service, action, region, params):
        if action != "DescribeBillDetailForOrganization":
            return api_call(service, action, region, params)
        if params["BusinessCode"] != "p_cvm":
            return {"DetailSet": [], "Total": 0}
        offset = params["Offset"]
        offsets.append(offset)
        rows = []
        for index in range(offset, min(offset + params["Limit"], total)):
            row = bill()
            if index < total - 1:
                row.update(
                    ResourceId=f"ins-outside-{index}",
                    FeeBeginTime="2026-11-19 12:00:00",
                    FeeEndTime="2026-11-19 13:00:00",
                )
            rows.append(row)
        return {"DetailSet": rows, "Total": total}

    snapshots = reader(profile, call).billing_candidates(DAY)
    assert offsets == list(range(0, total, 100))
    assert len(snapshots) == 1 and snapshots[0]["machine_id"] == "ins-one"


def test_bill_capacity_overlap_merge_does_not_bridge_gaps_or_sum_fees():
    row = {
        "machine_id": "np-one",
        "region": "ap-beijing",
        "cluster_id": "cls-9zpon9y1",
        "resource_class": "tke_native",
        "instance_type": "",
        "cpu_cores": 8,
        "memory_bytes": None,
        "observed_from": START,
        "observed_until": END,
    }
    memory = {**row, "cpu_cores": None, "memory_bytes": 32 * 1024**3}
    later = {
        **row,
        "observed_from": END + timedelta(minutes=2),
        "observed_until": END + timedelta(minutes=5),
    }
    merged = tencent.TencentReader._merge_snapshots([row, memory, row, later])
    assert len(merged) == 2 and merged[0]["cpu_cores"] == 8
    assert merged[0]["memory_bytes"] == 32 * 1024**3
    with pytest.raises(ValueError, match="Conflicting"):
        tencent.TencentReader._merge_snapshots([row, {**row, "cpu_cores": 16}])


def test_billing_catalog_missing_unknown_capacity_and_conflicting_type_rejected(profile):
    def call(service, action, region, params):
        if action == "DescribeInstanceTypeConfigs":
            return {"InstanceTypeConfigSet": []}
        return api_call(service, action, region, params)

    assert reader(profile, call).billing_candidates(DAY)[0]["cpu_cores"] is None
    source = reader(
        profile,
        lambda *a: {
            "InstanceTypeConfigSet": [
                {"InstanceType": "TYPE", "CPU": 2, "Memory": 4},
                {"InstanceType": "TYPE", "CPU": 4, "Memory": 4},
            ]
        },
    )
    with pytest.raises(ValueError, match="Conflicting"):
        source._type_capacity("ap-beijing", "TYPE")


def test_machine_and_pod_queries_are_independent_exact_and_normalized(profile):
    for kind in ("cvm", "tke_native", "pod"):
        call = Mock(side_effect=api_call)
        source = reader(profile, call)
        points = source.window(START, END, [target(kind)])
        assert len(points) == 2 and {p["metric"] for p in points} == {"cpu", "memory"}
        assert {p["entity"] for p in points} == {
            "uid-one" if kind == "pod" else target(kind)["machine_id"]
        }
        assert all(p["timestamp"].tzinfo == UTC and p["timestamp"] == START for p in points)
        assert {p["unit"] for p in points} == {"absolute" if kind == "pod" else "percent"}
        requests = [
            c.args[3]
            for c in call.call_args_list
            if c.args[1] in {"DescribeStatisticData", "GetMonitorData"}
        ]
        if kind == "pod":
            assert len(requests) == 1 and all(
                n.startswith("K8sPod") for n in requests[0]["MetricNames"]
            )
            dims = {d["Key"]: d["Value"] for d in requests[0]["Conditions"]}
            assert dims == {
                "tke_cluster_instance_id": ["cls-9zpon9y1"],
                "namespace": ["jenkins-tidb"],
                "pod_name": ["build-abc"],
                "node": ["10.0.6.207"],
            }
            assert next(p for p in points if p["metric"] == "memory")["value"] == 2_000_000
        elif kind == "cvm":
            assert len(requests) == 2 and all(r["SpecifyStatistics"] == 1 for r in requests)
        else:
            assert len(requests) == 1 and all(
                n.startswith("K8sNode") for n in requests[0]["MetricNames"]
            )
        assert source.stats["points"] == 2 and source.stats["response_bytes"] > 0


def test_ten_pods_each_get_an_exact_selector_request_and_both_metrics(profile):
    requests = []

    def call(service, action, region, params):
        if action == "DescribeStatisticData":
            conditions = params["Conditions"]
            assert all(c["Operator"] == "=" and len(c["Value"]) == 1 for c in conditions)
            dims = {c["Key"]: c["Value"][0] for c in conditions}
            requests.append(dims)
            return tke_response("pod", **dims)
        return api_call(service, action, region, params)

    targets = [target("pod", pod_uid=f"uid-{i}", pod_name=f"build-{i}") for i in range(10)]
    points = reader(profile, call).window(START, END, targets)
    assert len(requests) == 10
    assert {d["pod_name"] for d in requests} == {t["pod_name"] for t in targets}
    assert all(
        dims == {
            "tke_cluster_instance_id": "cls-9zpon9y1",
            "namespace": "jenkins-tidb",
            "pod_name": f"build-{i}",
            "node": "10.0.6.207",
        }
        for i, dims in enumerate(requests)
    )
    assert len(points) == 20
    assert {(p["entity"], p["metric"]) for p in points} == {
        (t["pod_uid"], metric) for t in targets for metric in ("cpu", "memory")
    }


def test_pod_instance_selector_and_uid_name_reuse_mapping(profile):
    snap = target(
        "pod", node_query_kind="un_instance_id", node_query_value="np-historical", pod_uid="uid-new"
    )

    def call(service, action, region, params):
        if action == "DescribeStatisticData":
            response = tke_response("pod", un_instance_id="np-historical")
            for metric in response["Data"]:
                metric["Points"][0]["Dimensions"] = [
                    d for d in metric["Points"][0]["Dimensions"] if d["Name"] != "node"
                ]
            return response
        return api_call(service, action, region, params)

    points = reader(profile, call).window(START, END, [snap])
    assert {p["entity"] for p in points} == {"uid-new"}


@pytest.mark.parametrize(
    "snap",
    [
        target("pod", node_query_kind=""),
        target("pod", node_query_value=""),
        target("pod", namespace="unapproved"),
        target("pod", cluster_id="cls-other"),
        target("pod", region="ap-hongkong"),
        target("pod", pod_uid=""),
        target("cvm", machine_id="np-other"),
        target("cvm", cluster_id="cls-other"),
        target("tke_native", resource_class="tke_cvm"),
        target("tke_native", region="ap-unknown"),
    ],
)
def test_invalid_target_fails_before_any_cloud_call(profile, snap):
    call = Mock()
    with pytest.raises(ValueError):
        reader(profile, call).window(START, END, [snap])
    call.assert_not_called()


@pytest.mark.parametrize(
    "begin,end",
    [
        (START, START),
        (START, START + timedelta(minutes=31)),
        (START + timedelta(seconds=1), END),
        (START - timedelta(days=8), END - timedelta(days=8)),
        (START + timedelta(days=1), END + timedelta(days=1)),
    ],
)
def test_time_limits_and_horizon_fail_before_io(profile, begin, end):
    call = Mock()
    with pytest.raises(ValueError):
        reader(profile, call).window(begin, end, [target("pod")])
    call.assert_not_called()


def test_empty_duplicate_targets_and_unknown_targets(profile):
    source = reader(profile)
    assert source.window(START, END, []) == [] and source.stats["calls"] == 0
    assert len(source.window(START, END, [target(), target()])) == 2
    with pytest.raises(ValueError, match="Ambiguous"):
        source.window(START, END, [target("pod"), target("pod", pod_name="another-name")])
    with pytest.raises(ValueError, match="budget"):
        source.window(START, END, [target()] * 10001)
    with pytest.raises(ValueError):
        source.window(START, END, [{"kind": "disk", "region": "ap-beijing"}])


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "2"])
def test_invalid_usage_values_fail_not_zero(profile, value):
    def call(service, action, region, params):
        if action == "DescribeStatisticData":
            return tke_response("pod", value)
        return api_call(service, action, region, params)

    with pytest.raises(ValueError):
        reader(profile, call).window(START, END, [target("pod")])


@pytest.mark.parametrize(
    "change", ["identity", "time", "alignment", "metric", "period", "cap", "truncated", "conflict"]
)
def test_tke_strict_identity_time_duplicate_and_truncation_checks(profile, change):
    response = tke_response("pod")
    series = response["Data"][0]["Points"][0]
    if change == "identity":
        series["Dimensions"][0]["Value"] = "cls-other"
    elif change == "time":
        series["Values"][0]["Timestamp"] = (START - timedelta(minutes=1)).timestamp()
    elif change == "alignment":
        series["Values"][0]["Timestamp"] += 1
    elif change == "metric":
        response["Data"][0]["MetricName"] = "DiskUsage"
    elif change == "period":
        response["Period"] = 300
    elif change == "cap":
        response["Data"][0]["Points"] *= 2000
    elif change == "truncated":
        response["Truncated"] = True
    else:
        series["Values"].append({"Timestamp": START.timestamp(), "Value": 9})

    def call(service, action, region, params):
        return (
            response
            if action == "DescribeStatisticData"
            else api_call(service, action, region, params)
        )

    with pytest.raises(ValueError):
        reader(profile, call).window(START, END, [target("pod")])


def test_exact_duplicates_deduplicate_and_nulls_remain_missing(profile):
    response = tke_response("pod")
    response["Data"][0]["Points"] *= 2
    for metric in response["Data"]:
        metric["Points"][0]["Values"][0]["Value"] = None

    def call(service, action, region, params):
        return (
            response
            if action == "DescribeStatisticData"
            else api_call(service, action, region, params)
        )

    assert reader(profile, call).window(START, END, [target("pod")]) == []


@pytest.mark.parametrize("change", ["values", "identity", "arrays", "period", "percent"])
def test_cvm_explicit_average_and_source_checks(profile, change):
    response = cvm_response()
    series = response["DataPoints"][0]
    if change == "values":
        series["Values"] = series.pop("AvgValues")
    elif change == "identity":
        series["Dimensions"][0]["Value"] = "ins-other"
    elif change == "arrays":
        series["AvgValues"].pop()
    elif change == "period":
        response["Period"] = 300
    else:
        series["AvgValues"][0] = 101

    def call(service, action, region, params):
        return response if action == "GetMonitorData" else api_call(service, action, region, params)

    with pytest.raises(ValueError):
        reader(profile, call).window(START, END, [target()])


def test_cvm_values_only_allowed_after_explicit_semantics_verification(profile):
    for entry in profile["contracts"]["cvm"].values():
        entry["contract"]["values_avg_verified"] = True
    repin(profile)

    def call(service, action, region, params):
        if action == "GetMonitorData":
            response = cvm_response()
            response["DataPoints"][0]["Values"] = response["DataPoints"][0].pop("AvgValues")
            return response
        return api_call(service, action, region, params)

    assert len(reader(profile, call).window(START, END, [target()])) == 2


@pytest.mark.parametrize("kind,namespace", [("cvm", "QCE/CVM"), ("tke_native", "QCE/TKE2")])
def test_machine_contract_cache_does_not_require_or_validate_pod_metadata(profile, kind, namespace):
    metadata_calls = []
    pod_available = False
    pod_point_calls = []

    def call(service, action, region, params):
        response = api_call(service, action, region, params)
        if action == "DescribeBaseMetrics":
            metadata_calls.append(params["Namespace"])
            if not pod_available:
                response["MetricSet"] = [
                    m for m in response["MetricSet"] if m["MetricName"] != "K8sPodMemWorkingSetBytes"
                ]
        if action == "DescribeStatisticData" and params["MetricNames"][0].startswith("K8sPod"):
            pod_point_calls.append(action)
        return response

    source = reader(profile, call)
    assert len(source.window(START, END, [target(kind)])) == 2
    assert len(source.window(START, END, [target(kind)])) == 2
    assert metadata_calls == [namespace]
    with pytest.raises(ValueError, match="contract differs"):
        source.window(START, END, [target("pod")])
    assert not pod_point_calls

    pod_available = True
    assert len(source.window(START, END, [target("pod")])) == 2
    assert len(source.window(START, END, [target("pod")])) == 2
    assert metadata_calls == [namespace, "QCE/TKE2", "QCE/TKE2"]


def test_account_and_readonly_actions(profile):
    source = reader(profile, lambda *args: {"AccountId": "other"})
    with pytest.raises(ValueError, match="Credential account"):
        source.verify_account()
    for service, action in (
        ("cvm", "DescribeInstances"),
        ("cvm", "TerminateInstances"),
        ("sts", "GetMonitorData"),
        ("billing", "DescribeBillDetail"),
    ):
        with pytest.raises(ValueError, match="Non-read-only"):
            source.call(service, action, "ap-beijing")
    with pytest.raises(ValueError, match="region"):
        source.call("sts", "GetCallerIdentity", "ap-unknown")


@pytest.mark.parametrize(
    "overrides",
    [
        {"Unit": "Bytes"},
        {"Periods": [{"Period": "300", "StatType": ["avg"]}]},
        {"Periods": [{"Period": "60", "StatType": ["max"]}]},
    ],
)
def test_target_metric_contract_mismatch_rejected_before_point_reads(profile, overrides):
    def call(service, action, region, params):
        assert action not in {"GetMonitorData", "DescribeStatisticData"}
        response = api_call(service, action, region, params)
        if action == "DescribeBaseMetrics":
            response["MetricSet"][0].update(overrides)
        return response

    with pytest.raises(ValueError, match="contract differs"):
        reader(profile, call).window(START, END, [target()])


def test_retry_attempt_qps_and_response_budgets(profile, monkeypatch):
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(tencent.time, "monotonic", lambda: clock[0])
    attempts = []

    def call(*args):
        attempts.append(clock[0])
        if len(attempts) == 1:
            return {"Error": {"Code": "RequestLimitExceeded", "Message": "not logged"}}
        if len(attempts) == 2:
            error = RuntimeError("offline transient")
            error.code = "ClientNetworkError"
            raise error
        return {"AccountId": tencent.ACCOUNT_ID}

    source = reader(profile, call, sleep)
    source.verify_account()
    source.call("sts", "GetCallerIdentity", "ap-beijing")
    assert attempts == pytest.approx([0, 1, 3, 3.2])
    assert sleeps == pytest.approx([1, 2, 0.2])
    assert source.stats["calls"] == 4
    source.stats["calls"] = 2000
    with pytest.raises(ValueError, match="request budget"):
        source.call("sts", "GetCallerIdentity", "ap-beijing")
    source = reader(profile, lambda *args: {"Error": {"Code": "InternalError"}})
    with pytest.raises(ValueError, match="Cloud returned"):
        source.call("sts", "GetCallerIdentity", "ap-beijing")
    assert source.stats["calls"] == 3
    source = reader(profile, lambda *args: {"Error": {"Code": "InvalidParameter"}})
    with pytest.raises(ValueError):
        source.call("sts", "GetCallerIdentity", "ap-beijing")
    assert source.stats["calls"] == 1
    source = reader(profile, lambda *args: {"blob": "x" * (16 * 1024**2)})
    with pytest.raises(ValueError, match="response budget"):
        source.call("sts", "GetCallerIdentity", "ap-beijing")
    source = reader(profile)
    source.stats["response_bytes"] = 1024**3
    with pytest.raises(ValueError, match="response budget"):
        source.call("sts", "GetCallerIdentity", "ap-beijing")


def test_sdk_uses_installed_common_client_without_network(profile, monkeypatch):
    from tencentcloud.common.abstract_client import AbstractClient

    source = tencent.TencentReader(profile, sleep=Mock())
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="credentials"):
        source.verify_account()
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "offline-test")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "offline-test-not-a-secret")
    monkeypatch.setattr(
        AbstractClient,
        "call_json",
        lambda *a, **kw: {"Response": {"AccountId": tencent.ACCOUNT_ID}},
    )
    source.verify_account()
    source.call("sts", "GetCallerIdentity", "ap-beijing")
    assert len(source._clients) == 1


def test_probe_unverified_profile_is_readonly_billing_and_capabilities_only():
    profile = tencent.load_profile(str(EXAMPLE), probe=True)
    call = Mock(side_effect=api_call)
    source = tencent.TencentReader(profile, call=call, sleep=Mock(), probe=True)
    result = tencent.probe(profile, reader=source, usage_date=DAY)
    assert result["dry_run"] and result["billing_candidates"] == 2 and result["metric_metadata"]
    assert not result["full_replay_horizon_verified"] and not result["unit_conversion_verified"]
    assert not any(
        c.args[1] in {"GetMonitorData", "DescribeStatisticData", "DescribeInstances"}
        for c in call.call_args_list
    )


@pytest.mark.parametrize("kind", ["cvm", "tke_native"])
def test_machine_queries_actually_batch_provider_requests(profile, kind):
    def call(service, action, region, params):
        if action == "GetMonitorData":
            ids = [item["Dimensions"][0]["Value"] for item in params["Instances"]]
            return {"Period": 60, "DataPoints": [cvm_response(entity)["DataPoints"][0] for entity in ids]}
        if action == "DescribeStatisticData":
            ids = next(c["Value"] for c in params["Conditions"] if c["Key"] == "un_instance_id")
            result = tke_response("tke_native")
            for metric in result["Data"]:
                series = metric["Points"][0]
                metric["Points"] = [{**series, "Dimensions": [
                    {**d, "Value": entity} if d["Name"] == "un_instance_id" else d
                    for d in series["Dimensions"]]} for entity in ids]
            return result
        return api_call(service, action, region, params)
    mock = Mock(side_effect=call)
    prefix = "ins" if kind == "cvm" else "np"
    targets = [target(kind, machine_id=f"{prefix}-{name}") for name in ("one", "two")]
    result = reader(profile, mock).window(START, END, targets)
    assert {p["entity"] for p in result} == {t["machine_id"] for t in targets}
    monitor_calls = [c for c in mock.call_args_list if c.args[1] in {"GetMonitorData", "DescribeStatisticData"}]
    assert len(monitor_calls) == (2 if kind == "cvm" else 1)


def test_configured_business_labels_are_not_a_fixed_schema(tmp_path):
    raw = json.loads(EXAMPLE.read_text())
    raw["labels"] = ["test_campaign"]
    path = tmp_path/"custom-labels.json"
    path.write_text(json.dumps(raw))
    assert tencent.load_profile(str(path), probe=True)["labels"] == ["test_campaign"]
