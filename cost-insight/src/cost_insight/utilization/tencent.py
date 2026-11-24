"""Read-only Tencent monitor adapter; no cluster client, discovery of members, or disks."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import UTC, datetime, timedelta

from cost_insight.utilization.aggregate import identity, utc
from cost_insight.utilization.policy import ACCOUNT_ID

VERSIONS = {"cvm": "2017-03-12", "monitor": "2018-07-24", "sts": "2018-08-13"}
ALLOWED_METRICS = {
    "cvm": {"cpu": "CpuUsage", "memory": "MemUsage"},
    "node": {
        "cpu": "K8sNodeCpuUsage",
        "memory": "K8sNodeMemUsage",
        "cpu_allocation": "K8sNodeCpuCoreRequestTotal",
        "memory_allocation": "K8sNodeMemoryRequestBytesTotal",
    },
    "pod": {"cpu": "K8sPodCpuCoreUsed", "memory": "K8sPodMemWorkingSetBytes"},
}


def load_profile(path: str, *, probe=False) -> dict:
    with open(path, encoding="utf-8") as handle:
        profile = json.load(handle)
    measurement = profile.pop("measurement")
    profile["contracts"] = build_contracts(
        measurement, include_tke_cvm=profile.pop("include_tke_cvm", False)
    )
    scope = {key: profile.get(key) for key in ("account_id", "cluster_id", "cluster_region")}
    scope.update(
        regions=sorted(profile.get("regions", [])), namespaces=sorted(profile.get("namespaces", []))
    )
    for metrics in profile["contracts"].values():
        for entry in metrics.values():
            entry["collection_scope"] = scope
            for contract in (entry["contract"], *entry.get("scope_contracts", {}).values()):
                contract["source_scope_hash"] = identity(scope)
    validate_profile(profile, require_conversion=not probe)
    return profile


def build_contracts(measurement: dict, *, include_tke_cvm=False) -> dict:
    """Pin one source contract catalogue; zero defaults are deliberately unverified."""
    if not isinstance(include_tke_cvm, bool):
        raise TypeError("include_tke_cvm must be a JSON boolean")
    result = {}
    for cls in ("cvm", "tke_native", "tke_cvm", "tke_unknown"):
        kind = "cvm" if cls == "cvm" else "node"
        result[cls] = {}
        for metric, name in ALLOWED_METRICS[kind].items():
            consumer_only = cls == "tke_unknown" or (cls == "tke_cvm" and not include_tke_cvm)
            if consumer_only and metric.endswith("allocation"):
                continue
            contract = {
                "namespace": "QCE/CVM" if cls == "cvm" else "QCE/TKE2",
                "metric": name,
                "period": 60,
                "source_stat": "avg",
                "unit": "%",
                "normalized_unit": "cores" if metric.startswith("cpu") else "bytes",
                "semantic_version": "v1",
                "denominator": "allocatable" if metric.endswith("allocation") else "physical",
                "timestamp_alignment": "bucket_start",
                "source_scale": 1,
            }
            if metric.endswith("allocation"):
                contract.update(
                    unit="None" if metric.startswith("cpu") else "MBytes",
                    source_scale=1
                    if metric.startswith("cpu")
                    else measurement.get("node_memory_request_source_scale", 0),
                )
            entry = {
                "contract": contract,
                "verified_horizon_seconds": measurement.get("verified_horizon_seconds", 0),
                "safety_margin_seconds": measurement.get("safety_margin_seconds", 3600),
                "horizon_verified_at": measurement.get("horizon_verified_at"),
                "evidence_reference": measurement.get("evidence_reference"),
                "timestamp_alignment_verified": measurement.get(
                    "timestamp_alignment_verified", False
                ),
                "consumer_only": consumer_only,
            }
            if cls != "cvm" and not metric.endswith("allocation"):
                pod = {
                    **contract,
                    "metric": ALLOWED_METRICS["pod"][metric],
                    "denominator": "requests",
                    "unit": "None" if metric == "cpu" else "MBytes",
                    "source_scale": 1
                    if metric == "cpu"
                    else measurement.get("pod_memory_source_scale", 0),
                }
                entry["scope_contracts"] = {"job": dict(pod), "repo": dict(pod)}
            result[cls][metric] = entry
    return result


def validate_profile(profile: dict, *, require_conversion=True) -> None:
    if profile.get("account_id") != ACCOUNT_ID:
        raise ValueError("Only the explicit Tencent CICD account is supported")
    regions = profile.get("regions")
    if (
        not isinstance(regions, list)
        or not 1 <= len(regions) <= 4
        or len(set(regions)) != len(regions)
    ):
        raise ValueError("Supply 1..4 explicit regions; region/account discovery is not supported")
    if any(not isinstance(r, str) or not r.startswith(("ap-", "na-", "eu-")) for r in regions):
        raise ValueError("Invalid explicit region")
    if (
        not profile.get("cluster_id", "").startswith("cls-")
        or profile.get("cluster_region") not in regions
    ):
        raise ValueError("One explicit cluster and its region are required")
    namespaces = profile.get("namespaces")
    if not isinstance(namespaces, list) or not namespaces or len(namespaces) > 32:
        raise ValueError("Explicit bounded CI namespaces are required")
    if any(not isinstance(n, str) or not n or len(n) > 63 for n in namespaces):
        raise ValueError("Invalid namespace")
    contracts = profile.get("contracts", {})
    if set(contracts) != {"cvm", "tke_native", "tke_cvm", "tke_unknown"}:
        raise ValueError("Explicit physical and unknown-consumer contracts are required")
    for cls, entries in contracts.items():
        for metric in ("cpu", "memory"):
            if metric not in entries:
                raise ValueError("CPU and memory contracts are required for each scope")
        for metric, entry in entries.items():
            kind = "cvm" if cls == "cvm" else "node"
            if (
                metric not in ALLOWED_METRICS[kind]
                or entry["contract"]["metric"] != ALLOWED_METRICS[kind][metric]
            ):
                raise ValueError("Only whitelisted compute mean metrics may be collected")
            if entry["contract"]["namespace"] != ("QCE/CVM" if kind == "cvm" else "QCE/TKE2"):
                raise ValueError("Namespace does not match collection API")
            from cost_insight.utilization.policy import validate_contract

            validate_contract(
                {
                    k: v
                    for k, v in entry["contract"].items()
                    if require_conversion or k != "source_scale"
                },
                metric,
                "account_class",
            )
            if (
                require_conversion
                and entry["contract"]["unit"] != "%"
                and not (
                    isinstance(entry["contract"].get("source_scale"), (int, float))
                    and not isinstance(entry["contract"]["source_scale"], bool)
                    and math.isfinite(entry["contract"]["source_scale"])
                    and entry["contract"]["source_scale"] > 0
                )
            ):
                raise ValueError("Node unit conversion requires a verified positive source_scale")
            if (cls == "tke_unknown" and not entry.get("consumer_only")) or (
                cls in {"cvm", "tke_native"} and entry.get("consumer_only")
            ):
                raise ValueError("Unknown class is consumer-only; CVM/native classes are physical")
            if cls == "tke_cvm" and bool(entry.get("consumer_only")) != bool(
                entries["cpu"].get("consumer_only")
            ):
                raise ValueError("TKE CVM physical/consumer mode must match across metrics")
            if cls != "cvm" and metric in {"cpu", "memory"}:
                for scope in ("job", "repo"):
                    contract = entry.get("scope_contracts", {}).get(scope, {})
                    if (
                        contract.get("metric") != ALLOWED_METRICS["pod"][metric]
                        or contract.get("namespace") != "QCE/TKE2"
                    ):
                        raise ValueError("Pod requests contracts are required for consumer scopes")
                    validate_contract(
                        {
                            k: v
                            for k, v in contract.items()
                            if require_conversion or k != "source_scale"
                        },
                        metric,
                        scope,
                    )
                    if require_conversion and (
                        not isinstance(contract.get("source_scale"), (int, float))
                        or isinstance(contract["source_scale"], bool)
                        or not math.isfinite(contract["source_scale"])
                        or contract["source_scale"] <= 0
                    ):
                        raise ValueError(
                            "Pod unit conversion requires a verified positive source_scale"
                        )
                reference = contracts["tke_native"][metric]["scope_contracts"]["job"]
                if any(entry["scope_contracts"][s] != reference for s in ("job", "repo")):
                    raise ValueError("All consumer scopes must share the same Pod source semantics")


class TencentReader:
    """SDK-backed reader with explicit per-run request/byte budgets and bounded retries."""

    def __init__(self, profile: dict, *, call=None, sleep=time.sleep, probe=False):
        validate_profile(profile, require_conversion=not probe)
        self.profile = profile
        self._call_override = call
        self._sleep = sleep
        self._clients = {}
        self._contracts_verified = False
        self.stats = {"calls": 0, "response_bytes": 0, "points": 0, "unmapped_node_series": 0}

    def _sdk_call(self, service, action, region, params):
        from tencentcloud.common.abstract_client import AbstractClient
        from tencentcloud.common.credential import Credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile

        key = (service, region)
        if key not in self._clients:
            # Explicit scoped environment credentials; no organization/metadata traversal.
            secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID")
            secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY")
            if not secret_id or not secret_key:
                raise ValueError("Read-only Tencent environment credentials are required")
            credential = Credential(secret_id, secret_key, os.environ.get("TENCENTCLOUD_TOKEN"))
            profile = ClientProfile(httpProfile=HttpProfile(reqTimeout=20))
            profile.disable_region_breaker = True
            # SDK DEBUG logs contain whole responses; never emit them for this job.
            logging.getLogger("tencentcloud_sdk_common").setLevel(logging.WARNING)

            class ServiceClient(AbstractClient):
                _endpoint = service + ".tencentcloudapi.com"
                _apiVersion = VERSIONS[service]
                _service = service

            self._clients[key] = ServiceClient(credential, region, profile)
        return self._clients[key].call_json(action, params)

    def call(self, service, action, region, **params):
        if service not in VERSIONS or action not in {
            "DescribeInstances",
            "GetMonitorData",
            "DescribeStatisticData",
            "DescribeBaseMetrics",
            "GetCallerIdentity",
        }:
            raise ValueError("Non-read-only API action rejected")
        for attempt in range(3):
            if self.stats["calls"] >= 2000:
                raise ValueError("Collection request budget exhausted")
            self.stats["calls"] += 1
            try:
                response = (self._call_override or self._sdk_call)(service, action, region, params)
                response = response.get("Response", response)
                if response.get("Error"):
                    raise ValueError("Cloud returned an error; previous daily facts must remain")
                size = len(json.dumps(response, separators=(",", ":")).encode())
                self.stats["response_bytes"] += size
                if size > 16 * 1024**2 or self.stats["response_bytes"] > 1024**3:
                    raise ValueError("Collection response budget exceeded")
                return response
            except Exception as exc:
                code = getattr(exc, "code", "")
                if attempt == 2 or not code.startswith(
                    ("RequestLimitExceeded", "InternalError", "ClientNetworkError")
                ):
                    raise
                self._sleep(2**attempt)
        raise AssertionError("unreachable")

    def verify_account(self):
        result = self.call("sts", "GetCallerIdentity", self.profile["cluster_region"])
        if str(result.get("AccountId")) != ACCOUNT_ID:
            raise ValueError("Credential account does not match configured CICD account")

    def verify_contracts(self):
        if self._contracts_verified:
            return
        for namespace in ("QCE/CVM", "QCE/TKE2"):
            response = self.call(
                "monitor",
                "DescribeBaseMetrics",
                self.profile["cluster_region"],
                Namespace=namespace,
            )
            metadata = {m["MetricName"]: m for m in response.get("MetricSet", [])}
            pinned = [
                contract
                for metrics in self.profile["contracts"].values()
                for entry in metrics.values()
                for contract in [entry["contract"], *entry.get("scope_contracts", {}).values()]
                if contract["namespace"] == namespace
            ]
            for contract in pinned:
                metric = metadata.get(contract["metric"], {})
                periods = metric.get("Periods", [])
                if str(metric.get("Unit")) != contract["unit"] or not any(
                    str(p.get("Period")) == "60" and "avg" in p.get("StatType", []) for p in periods
                ):
                    raise ValueError(
                        "Live metric unit/statistic contract differs from pinned semantics"
                    )
        self._contracts_verified = True

    def inventory(self, now: datetime) -> list[dict]:
        result = []
        for region in self.profile["regions"]:
            offset = 0
            while True:
                page = self.call("cvm", "DescribeInstances", region, Offset=offset, Limit=100)
                instances = page.get("InstanceSet")
                if not isinstance(instances, list) or not isinstance(page.get("TotalCount"), int):
                    raise TypeError("Incomplete CVM inventory response")
                for item in instances:
                    result.append(
                        {
                            "resource_type": "cvm",
                            "resource_id": item["InstanceId"],
                            "provider_id": item["InstanceId"],
                            "cluster": "",
                            "namespace": "",
                            "name": "",
                            "observed_from": utc(now).replace(tzinfo=None),
                            "last_seen": utc(now).replace(tzinfo=None),
                            "observed_until": None,
                            "attributes_json": {
                                "cpu_capacity": float(item["CPU"]),
                                "memory_capacity": float(item["Memory"]) * 1024**3,
                                "instance_type": item.get("InstanceType", ""),
                                "resource_class": "cvm",
                                "region": region,
                                "created_at": item.get("CreatedTime"),
                                "metadata_confidence": "observed_inventory",
                            },
                        }
                    )
                offset += len(instances)
                if offset >= page["TotalCount"]:
                    break
                if not instances or offset >= 10_000:
                    raise ValueError("CVM inventory pagination incomplete or over budget")
        return result

    def _tke(self, start, end, metrics, conditions):
        return self.call(
            "monitor",
            "DescribeStatisticData",
            self.profile["cluster_region"],
            Module="monitor",
            Namespace="QCE/TKE2",
            MetricNames=metrics,
            Period=60,
            StartTime=start.isoformat(),
            EndTime=end.isoformat(),
            Conditions=conditions,
        )

    def window(self, start: datetime, end: datetime, observations: list[dict]) -> list[dict]:
        if not timedelta(0) < end - start <= timedelta(minutes=30):
            raise ValueError("Read windows must be <=30 minutes")
        points = []
        base_conditions = [
            {
                "Key": "tke_cluster_instance_id",
                "Operator": "=",
                "Value": [self.profile["cluster_id"]],
            }
        ]
        node_metrics = sorted(
            {
                entry["contract"]["metric"]
                for cls in ("tke_native", "tke_cvm")
                for entry in self.profile["contracts"].get(cls, {}).values()
            }
        )
        # Read all historical node series for the window, including disappeared nodes.
        # Cluster-only (no GroupBys) was verified for Node metrics; Pod metrics
        # require the explicit node batches below. Never request aggregated series.
        node = self._tke(start, end, node_metrics, base_conditions)
        node_ids = self._parse_tke(node, "node", points, start, end)
        for offset in range(0, len(node_ids), 10):
            conditions = base_conditions + [
                {
                    "Key": "un_instance_id",
                    "Operator": "in",
                    "Value": node_ids[offset : offset + 10],
                },
                {"Key": "namespace", "Operator": "in", "Value": self.profile["namespaces"]},
            ]
            response = self._tke(start, end, list(ALLOWED_METRICS["pod"].values()), conditions)
            self._parse_tke(response, "pod", points, start, end)
        cvms: dict[str, set[str]] = {}
        for obs in observations:
            if obs["resource_type"] != "cvm":
                continue
            attrs = obs["attributes_json"]
            attrs = json.loads(attrs) if isinstance(attrs, str) else attrs
            cvms.setdefault(attrs["region"], set()).add(obs["provider_id"])
        # With no separate physical TKE-CVM class, retain its machine usage in
        # the ordinary CVM scope. Node IDs come from historical monitor series,
        # so a short-lived host missed by the inventory snapshot is still queried.
        if self.profile["contracts"]["tke_cvm"]["cpu"].get("consumer_only"):
            cvms.setdefault(self.profile["cluster_region"], set()).update(
                i for i in node_ids if i.startswith("ins-")
            )
        for region, instance_set in cvms.items():
            ids = sorted(instance_set)
            for offset in range(0, len(ids), 50):
                for metric, provider_metric in ALLOWED_METRICS["cvm"].items():
                    response = self.call(
                        "monitor",
                        "GetMonitorData",
                        region,
                        Namespace="QCE/CVM",
                        MetricName=provider_metric,
                        Period=60,
                        StartTime=start.isoformat(),
                        EndTime=end.isoformat(),
                        Instances=[
                            {"Dimensions": [{"Name": "InstanceId", "Value": i}]}
                            for i in ids[offset : offset + 50]
                        ],
                    )
                    if response.get("Period") != 60 or not isinstance(
                        response.get("DataPoints"), list
                    ):
                        raise ValueError("CVM source period/response mismatch")
                    for series in response["DataPoints"]:
                        dims = {d["Name"]: d["Value"] for d in series["Dimensions"]}
                        if dims.get("InstanceId") not in instance_set or len(
                            series["Timestamps"]
                        ) != len(series["Values"]):
                            raise ValueError("Unexpected CVM metric series")
                        for stamp, value in zip(
                            series["Timestamps"], series["Values"], strict=True
                        ):
                            timestamp = datetime.fromtimestamp(stamp, UTC)
                            if start <= timestamp < end:
                                points.append(
                                    {
                                        "kind": "cvm",
                                        "entity": dims["InstanceId"],
                                        "metric": metric,
                                        "timestamp": timestamp,
                                        "value": value,
                                        "region": region,
                                    }
                                )
        self.stats["points"] += len(points)
        if len(points) > 250_000:
            raise ValueError("Window point budget exceeded")
        return points

    def _parse_tke(self, response, kind, points, start, end):
        if response.get("Period") != 60 or not isinstance(response.get("Data"), list):
            raise ValueError("TKE source period/response mismatch")
        reverse = {v: k for k, v in ALLOWED_METRICS[kind].items()}
        nodes = set()
        for metric in response["Data"]:
            if metric["MetricName"] not in reverse:
                raise ValueError("Unrequested TKE metric")
            for series in metric["Points"]:
                dims = {d["Name"]: d["Value"] for d in series["Dimensions"]}
                node_id = dims.get("un_instance_id") or dims.get("node") or ""
                if (
                    not isinstance(node_id, str)
                    or len(node_id) > 255
                    or (kind == "node" and not node_id)
                ):
                    raise ValueError(
                        "Missing or oversized node identity; aggregated series are unsupported"
                    )
                if dims.get("tke_cluster_instance_id") != self.profile["cluster_id"]:
                    raise ValueError("Unexpected TKE cluster series")
                if node_id:
                    nodes.add(node_id)
                if kind == "node" and not node_id.startswith(("np-", "ins-")):
                    self.stats["unmapped_node_series"] += 1
                if kind == "pod" and dims.get("namespace") not in self.profile["namespaces"]:
                    raise ValueError("Unexpected namespace series")
                entity = (
                    node_id
                    if kind == "node"
                    else "|".join((self.profile["cluster_id"], dims["namespace"], dims["pod_name"]))
                )
                for value in series["Values"]:
                    timestamp = datetime.fromtimestamp(value["Timestamp"], UTC)
                    if start <= timestamp < end:
                        points.append(
                            {
                                "kind": kind,
                                "entity": entity,
                                "metric": reverse[metric["MetricName"]],
                                "timestamp": timestamp,
                                "value": value["Value"],
                                "node_id": node_id,
                                "namespace": dims.get("namespace", ""),
                                "region": self.profile["cluster_region"],
                            }
                        )
        return sorted(nodes)


def probe(profile, *, reader=None, now=None, usage_date=None):
    """Read-only capability sample, NOT proof of a full-day replay horizon."""
    reader = reader or TencentReader(profile, probe=True)
    reader.verify_account()
    end = utc(now or datetime.now(UTC)).replace(second=0, microsecond=0) - timedelta(minutes=10)
    start = end - timedelta(minutes=20)
    if usage_date is not None:
        from cost_insight.utilization.policy import day_start

        start = utc(day_start(usage_date))
        end = start + timedelta(minutes=20)
    inventory = reader.inventory(end)
    metadata = []
    for kind, namespace in (("cvm", "QCE/CVM"), ("node", "QCE/TKE2")):
        response = reader.call(
            "monitor", "DescribeBaseMetrics", profile["cluster_region"], Namespace=namespace
        )
        expected = set(ALLOWED_METRICS[kind].values())
        if kind == "node":
            expected.update(ALLOWED_METRICS["pod"].values())
        metadata.extend(
            {
                "namespace": namespace,
                "metric": m["MetricName"],
                "unit": m.get("Unit"),
                "periods": m.get("Periods"),
            }
            for m in response.get("MetricSet", [])
            if m["MetricName"] in expected
        )
    points = reader.window(start, end, inventory)
    return {
        "dry_run": True,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "inventory_resources": len(inventory),
        "source": reader.stats,
        "metric_metadata": metadata,
        "sampled_points": len(points),
        "full_replay_horizon_verified": False,
        "unit_conversion_verified": False,
        "notice": "One window is not a full-day horizon or attribution/cleanup canary; no database was opened",
    }
