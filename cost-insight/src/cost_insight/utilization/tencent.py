"""Bounded, read-only Tencent history and independent machine/Pod monitor reads."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from cost_insight.utilization.policy import ACCOUNT_ID, label_keys

VERSIONS = {
    "billing": "2018-07-09",
    "cvm": "2017-03-12",
    "monitor": "2018-07-24",
    "sts": "2018-08-13",
}
ACTIONS = {
    "billing": {"DescribeBillDetailForOrganization"},
    "cvm": {"DescribeInstanceTypeConfigs"},
    "monitor": {"GetMonitorData", "DescribeStatisticData", "DescribeBaseMetrics"},
    "sts": {"GetCallerIdentity"},
}
ALLOWED_METRICS = {
    "cvm": {"cpu": "CpuUsage", "memory": "MemUsage"},
    "tke_native": {"cpu": "K8sNodeCpuUsage", "memory": "K8sNodeMemUsage"},
    "pod": {"cpu": "K8sPodCpuCoreUsed", "memory": "K8sPodMemWorkingSetBytes"},
}
REGION_IDS = {"5": "ap-hongkong", "8": "ap-beijing"}
BILLING_ROW_LIMIT = 30_000  # Per product scan, including non-target rows in a month-wide reply.
BEIJING = ZoneInfo("Asia/Shanghai")
# Exact observed resource/service components, not a speculative family-name matcher.
NATIVE_COMPONENTS = {
    f"sv_011691_{family}_{metric}{suffix}": ("cpu" if metric == "cpu" else "memory")
    for family, fee in (("hn_sa2", "_fee"), ("nn_bf1", ""), ("nn_sa9", ""))
    for metric in ("cpu", "mem")
    for suffix in (fee, "_servicefee")
}


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _utc(value):
    if not isinstance(value, datetime):
        raise TypeError("Expected a UTC datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite numeric quantity")
    if value < 0 or (positive and value == 0):
        raise ValueError("Expected a nonnegative/positive quantity")
    return float(value)


def collection_scope(profile):
    return {
        **{k: profile.get(k) for k in ("account_id", "cluster_id", "cluster_region")},
        **{k: sorted(profile.get(k, [])) for k in ("regions", "namespaces", "labels")},
    }


def contract_version(profile):
    return _hash({"contracts": profile["contracts"], "scope": collection_scope(profile)})


def build_contracts(measurement: dict) -> dict:
    """No verified scale, alignment or horizon is inferred from a successful sample."""
    result = {}
    for kind, metrics in ALLOWED_METRICS.items():
        result[kind] = {}
        for metric, name in metrics.items():
            result[kind][metric] = {
                "contract": {
                    "namespace": "QCE/CVM" if kind == "cvm" else "QCE/TKE2",
                    "metric": name,
                    "period": 60,
                    "source_stat": "avg",
                    "unit": "%" if kind != "pod" else "None" if metric == "cpu" else "MBytes",
                    "normalized_unit": "cores" if metric == "cpu" else "bytes",
                    "denominator": "requests" if kind == "pod" else "physical",
                    "semantic_version": "v2",
                    "timestamp_alignment": "bucket_start",
                    "source_scale": measurement.get("source_scales", {})
                    .get(kind, {})
                    .get(metric, 0),
                    "values_avg_verified": False,
                },
                "verified_horizon_seconds": measurement.get("verified_horizon_seconds", 0),
                "safety_margin_seconds": measurement.get("safety_margin_seconds", 3600),
                "horizon_verified_at": measurement.get("horizon_verified_at"),
                "evidence_reference": measurement.get("evidence_reference"),
                "timestamp_alignment_verified": measurement.get(
                    "timestamp_alignment_verified", False
                ),
            }
    return result


def load_profile(path: str, *, probe=False) -> dict:
    with open(path, encoding="utf-8") as handle:
        profile = json.load(handle)
    profile["contracts"] = build_contracts(profile.pop("measurement"))
    scope_hash = _hash(collection_scope(profile))
    for metrics in profile["contracts"].values():
        for entry in metrics.values():
            entry["contract"]["source_scope_hash"] = scope_hash
    profile["contract_version"] = contract_version(profile)
    validate_profile(profile, require_conversion=not probe)
    return profile


def validate_profile(profile: dict, *, require_conversion=True) -> None:
    if profile.get("account_id") != ACCOUNT_ID:
        raise ValueError("Only the explicit Tencent CICD account is supported")
    for field, limit in (("regions", 4), ("namespaces", 32), ("labels", 32)):
        values = profile.get(field)
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= limit
            or any(not isinstance(v, str) for v in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError(f"Supply bounded explicit {field}")
    if any(not re.fullmatch(r"(?:ap|na|eu)-[a-z]+(?:-[0-9]+)?", r) for r in profile["regions"]):
        raise ValueError("Invalid explicit region")
    if (
        not re.fullmatch(r"cls-[a-z0-9]+", profile.get("cluster_id", ""))
        or profile.get("cluster_region") not in profile["regions"]
    ):
        raise ValueError("One explicit cluster and its region are required")
    if any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", n) for n in profile["namespaces"]
    ):
        raise ValueError("Invalid namespace")
    label_keys(profile["labels"])
    contracts = profile.get("contracts", {})
    if set(contracts) != set(ALLOWED_METRICS):
        raise ValueError("Only cvm, tke_native and pod CPU/memory contracts are supported")
    scope_hash = _hash(collection_scope(profile))
    for kind, metrics in contracts.items():
        if set(metrics) != {"cpu", "memory"}:
            raise ValueError("Only CPU and memory contracts are supported")
        expected = build_contracts({})[kind]
        for metric, entry in metrics.items():
            contract = entry["contract"]
            if any(
                contract.get(k) != v
                for k, v in expected[metric]["contract"].items()
                if k not in {"source_scale", "values_avg_verified"}
            ):
                raise ValueError("Metric contract differs from pinned V2 semantics")
            if contract.get("source_scope_hash") != scope_hash:
                raise ValueError("Contract collection scope hash mismatch")
            if not isinstance(contract.get("values_avg_verified"), bool):
                raise TypeError("Values average semantics must be explicitly verified")
            if "scope_contracts" in entry or "consumer_only" in entry:
                raise ValueError("V1 group/consumer contracts are unsupported")
            if require_conversion:
                scale = _number(contract.get("source_scale"), positive=True)
                if (kind != "pod" or metric == "cpu") and scale != 1:
                    raise ValueError("Percent and CPU core source_scale must equal 1")
                horizon = _number(entry.get("verified_horizon_seconds"), positive=True)
                margin = _number(entry.get("safety_margin_seconds"))
                if margin >= horizon or entry.get("timestamp_alignment_verified") is not True:
                    raise ValueError("Unverified source horizon/alignment")
                if not entry.get("horizon_verified_at") or not entry.get("evidence_reference"):
                    raise ValueError("Source horizon canary evidence is required")
                verified = datetime.fromisoformat(entry["horizon_verified_at"])
                if verified.tzinfo is None or _utc(verified) > datetime.now(UTC) + timedelta(
                    seconds=60
                ):
                    raise ValueError("Invalid source horizon verification time")
    if profile.get("contract_version") != contract_version(profile):
        raise ValueError("Contract version does not match contracts and collection scope")


class TencentReader:
    """SDK reads with attempt/QPS/response limits; no live inventory or Node-first discovery."""

    def __init__(self, profile: dict, *, call=None, sleep=time.sleep, probe=False):
        validate_profile(profile, require_conversion=not probe)
        self.profile = profile
        self._call_override = call
        self._sleep = sleep
        self._clients = {}
        self._verified_contract_kinds = set()
        self._account_verified = False
        self._lock = threading.Lock()
        self._last_attempt = None
        self._type_cache = {}
        self.stats = {"calls": 0, "response_bytes": 0, "points": 0}

    def _sdk_call(self, service, action, region, params):
        from tencentcloud.common.abstract_client import AbstractClient
        from tencentcloud.common.credential import Credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile

        key = (service, region)
        if key not in self._clients:
            secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID")
            secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY")
            if not secret_id or not secret_key:
                raise ValueError("Read-only Tencent environment credentials are required")
            credential = Credential(secret_id, secret_key, os.environ.get("TENCENTCLOUD_TOKEN"))
            profile = ClientProfile(httpProfile=HttpProfile(reqTimeout=20))
            profile.disable_region_breaker = True
            # SDK DEBUG logs can include complete source responses.
            logging.getLogger("tencentcloud_sdk_common").setLevel(logging.WARNING)

            class ServiceClient(AbstractClient):
                _endpoint = service + ".tencentcloudapi.com"
                _apiVersion = VERSIONS[service]
                _service = service

            self._clients[key] = ServiceClient(credential, region, profile)
        return self._clients[key].call_json(action, params)

    def call(self, service, action, region, **params):
        if action not in ACTIONS.get(service, set()):
            raise ValueError("Non-read-only API action rejected")
        if region not in self.profile["regions"]:
            raise ValueError("API region is outside configured scope")
        for attempt in range(3):
            with self._lock:
                if self.stats["calls"] >= 2000:
                    raise ValueError("Collection request budget exhausted")
                if self._last_attempt is not None:
                    delay = 0.2 - (time.monotonic() - self._last_attempt)
                    if delay > 0:
                        self._sleep(delay)
                self._last_attempt = time.monotonic()
                self.stats["calls"] += 1
            try:
                response = (self._call_override or self._sdk_call)(service, action, region, params)
            except Exception as exc:
                code = getattr(exc, "code", "")
                if not self._transient(code) or attempt == 2:
                    raise
                self._sleep(2**attempt)
                continue
            if not isinstance(response, dict):
                raise TypeError("Malformed cloud response")
            # Count envelopes and failed responses too; never log their contents.
            size = len(json.dumps(response, separators=(",", ":")).encode())
            with self._lock:
                self.stats["response_bytes"] += size
                if size > 16 * 1024**2 or self.stats["response_bytes"] > 1024**3:
                    raise ValueError("Collection response budget exceeded")
            response = response.get("Response", response)
            if not isinstance(response, dict):
                raise TypeError("Malformed cloud response")
            if response.get("Error"):
                code = response["Error"].get("Code", "")
                if self._transient(code) and attempt < 2:
                    self._sleep(2**attempt)
                    continue
                raise ValueError("Cloud returned an error; no source result may be applied")
            return response
        raise AssertionError("unreachable")

    @staticmethod
    def _transient(code):
        return isinstance(code, str) and code.split(".")[0] in {
            "RequestLimitExceeded",
            "InternalError",
            "ClientNetworkError",
        }

    def verify_account(self):
        if not self._account_verified:
            result = self.call("sts", "GetCallerIdentity", self.profile["cluster_region"])
            if str(result.get("AccountId")) != ACCOUNT_ID:
                raise ValueError("Credential account does not match configured CICD account")
            self._account_verified = True

    def _verify_contracts(self, kinds):
        by_namespace = {}
        for kind in sorted(kinds - self._verified_contract_kinds):
            for entry in self.profile["contracts"][kind].values():
                contract = entry["contract"]
                by_namespace.setdefault(contract["namespace"], {})[contract["metric"]] = contract
        for namespace, contracts in by_namespace.items():
            response = self.call(
                "monitor",
                "DescribeBaseMetrics",
                self.profile["cluster_region"],
                Namespace=namespace,
            )
            metadata = {}
            for item in response.get("MetricSet", []):
                name = item.get("MetricName")
                if name not in contracts:
                    continue
                if name in metadata:
                    raise ValueError("Duplicate metric metadata")
                metadata[name] = item
            for name, contract in contracts.items():
                metric = metadata.get(name, {})
                if str(metric.get("Unit")) != contract["unit"] or not any(
                    str(p.get("Period")) == str(contract["period"])
                    and contract["source_stat"] in p.get("StatType", [])
                    for p in metric.get("Periods", [])
                ):
                    raise ValueError(
                        "Live metric unit/statistic contract differs from pinned semantics"
                    )
        # QCE/TKE2 includes both native-machine and Pod metrics; verifying one
        # kind does not verify another, even if they used the same namespace.
        self._verified_contract_kinds.update(kinds)

    def billing_candidates(self, bill_day: date) -> list[dict]:
        """Return fee-interval snapshots for one BillDay after both product scans complete."""
        if not isinstance(bill_day, date) or isinstance(bill_day, datetime):
            raise TypeError("Expected a Beijing BillDay")
        self.verify_account()
        snapshots = []
        page_size = 100
        for product in ("p_cvm", "p_011691"):
            offset, expected_total, context = 0, 0, None
            pages_seen = set()
            while True:
                params = {
                    "BeginTime": f"{bill_day.isoformat()} 00:00:00",
                    "EndTime": f"{bill_day.isoformat()} 23:59:59",
                    "BusinessCode": product,
                    "Offset": offset,
                    "Limit": page_size,
                    "NeedRecordNum": int(offset == 0),
                }
                if context:
                    params["Context"] = context
                page = self.call(
                    "billing",
                    "DescribeBillDetailForOrganization",
                    self.profile["cluster_region"],
                    **params,
                )
                rows = page.get("DetailSet")
                if (
                    not isinstance(rows, list)
                    or len(rows) > page_size
                    or offset + len(rows) > BILLING_ROW_LIMIT
                ):
                    raise ValueError("Incomplete or over-budget billing pagination")
                if offset == 0:
                    count = page.get("Total")
                    if type(count) is not int or not 0 <= count <= BILLING_ROW_LIMIT:
                        raise ValueError("Invalid or over-budget billing total")
                    expected_total = count
                page_hash = _hash(rows)
                if rows and page_hash in pages_seen:
                    raise ValueError("Repeated billing page; complete scan cannot be established")
                pages_seen.add(page_hash)
                for row in rows:
                    if str(row.get("OwnerUin")) != ACCOUNT_ID:
                        raise ValueError("Billing OwnerUin does not match configured account")
                    if row.get("BusinessCode") != product:
                        raise ValueError("Unexpected billing product")
                    value = row.get("BillDay")
                    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                        raise ValueError("Missing or invalid billing BillDay")
                    try:
                        partition = date.fromisoformat(value)
                    except ValueError as exc:
                        raise ValueError("Invalid billing BillDay") from exc
                    # Some product-filtered requests return the bill month. All
                    # rows still count toward pagination, but not toward usage.
                    if partition.replace(day=1) != bill_day.replace(day=1):
                        raise ValueError("Billing BillDay is outside the requested month")
                    if partition != bill_day:
                        continue
                    left, right = (
                        self._fee_time(row.get("FeeBeginTime")),
                        self._fee_time(row.get("FeeEndTime")),
                    )
                    if right <= left:
                        raise ValueError("Invalid charge interval")
                    snapshot = self._bill_snapshot(row, product)
                    if snapshot is not None:
                        snapshots.append(
                            {
                                **snapshot,
                                "observed_from": left.replace(tzinfo=None),
                                "observed_until": right.replace(tzinfo=None),
                            }
                        )
                offset += len(rows)
                # Like the billing importer, only a short page proves EOF: Total
                # may be cached below the actual row count. A shortfall relative
                # to the first count is still evidence of possible truncation.
                if len(rows) < page_size:
                    if offset < expected_total:
                        raise ValueError(
                            "Billing pagination incomplete: shortfall against reported total"
                        )
                    break
                if offset >= BILLING_ROW_LIMIT:
                    raise ValueError("Billing pagination incomplete or over budget")
                context = page.get("Context")
                if context is not None and (not isinstance(context, str) or len(context) > 65536):
                    raise ValueError("Invalid billing pagination context")
        return self._merge_snapshots(snapshots)

    @staticmethod
    def _fee_time(value):
        if not isinstance(value, str):
            raise TypeError("Missing billing charge time")
        stamp = datetime.fromisoformat(value)
        # Tencent's zone-less bill times are Beijing local, unlike our DB UTC-naive times.
        return (
            stamp.replace(tzinfo=BEIJING).astimezone(UTC) if stamp.tzinfo is None else _utc(stamp)
        )

    def _bill_snapshot(self, row, product):
        components = row.get("ComponentSet")
        if not isinstance(components, list) or not components:
            raise ValueError("Missing billing components")
        configs = {}
        for component in components:
            values = component.get("ComponentConfig") or []
            if not isinstance(values, list):
                raise TypeError("Invalid billing component config")
            for item in values:
                name, value = item.get("Name"), item.get("Value")
                if name in {"地域", "机型"}:
                    if name in configs and configs[name] != value:
                        raise ValueError("Conflicting billing component config")
                    configs[name] = value
        region = configs.get("地域") or REGION_IDS.get(str(row.get("RegionId")))
        if not region:
            raise ValueError("Unsupported billing region mapping")
        if region not in self.profile["regions"]:
            raise ValueError("Billing region is outside configured scope")
        mapped = REGION_IDS.get(str(row.get("RegionId")))
        if mapped and mapped != region:
            raise ValueError("Conflicting billing region evidence")
        resource = row.get("ResourceId", "")
        instance_type = configs.get("机型", "")
        if not isinstance(instance_type, str) or len(instance_type) > 128:
            raise ValueError("Invalid historical machine instance type")
        if product == "p_cvm":
            if not isinstance(resource, str) or not re.fullmatch(r"ins-[a-z0-9]+", resource):
                raise ValueError("Invalid CVM billing identity")
            cpu, memory = (
                self._type_capacity(region, instance_type) if instance_type else (None, None)
            )
            cluster, kind, machine = "", "cvm", resource
        else:
            if not isinstance(resource, str) or not re.fullmatch(
                r"cls-[a-z0-9]+_np-[a-z0-9-]+", resource
            ):
                raise ValueError("Invalid native billing machine identity")
            cluster, machine = resource.split("_", 1)
            if cluster != self.profile["cluster_id"] or region != self.profile["cluster_region"]:
                raise ValueError("Native billing machine is outside configured cluster")
            capacities = {}
            for component in components:
                code = component.get("ItemCode", "")
                metric = NATIVE_COMPONENTS.get(code)
                if metric is None:
                    if isinstance(code, str) and re.search(r"_(cpu|mem)(?:_|$)", code):
                        raise ValueError("Unreviewed native capacity component")
                    continue  # Disk/network charges are never capacity evidence.
                unit = "核" if metric == "cpu" else "GiB"
                if component.get("UsedAmountUnit") != unit:
                    raise ValueError("Native billing capacity unit mismatch")
                try:
                    value = float(component["UsedAmount"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("Invalid native billing capacity quantity") from None
                if isinstance(component["UsedAmount"], bool):
                    raise TypeError("Invalid native billing capacity quantity")
                value = _number(_number(value, positive=True) * (1 if metric == "cpu" else 1024**3), positive=True)
                if metric in capacities and capacities[metric] != value:
                    raise ValueError("Conflicting native resource/service capacity quantities")
                capacities[metric] = value
            if not capacities:
                return None
            cpu, memory = capacities.get("cpu"), capacities.get("memory")
            kind = "tke_native"
        return {
            "machine_id": machine,
            "region": region,
            "cluster_id": cluster,
            "resource_class": kind,
            "instance_type": instance_type,
            "cpu_cores": cpu,
            "memory_bytes": memory,
        }

    def _type_capacity(self, region, instance_type):
        key = (region, instance_type)
        if key not in self._type_cache:
            response = self.call(
                "cvm",
                "DescribeInstanceTypeConfigs",
                region,
                Filters=[{"Name": "instance-type", "Values": [instance_type]}],
            )
            entries = response.get("InstanceTypeConfigSet")
            if not isinstance(entries, list):
                raise ValueError("Incomplete historical instance type catalogue")
            capacities = set()
            for item in entries:
                if item.get("InstanceType") != instance_type:
                    raise ValueError("Unexpected instance type catalogue identity")
                capacities.add(
                    (
                        _number(item.get("CPU"), positive=True),
                        _number(_number(item.get("Memory"), positive=True) * 1024**3, positive=True),
                    )
                )
            if len(capacities) > 1:
                raise ValueError("Conflicting historical instance type capacity")
            self._type_cache[key] = next(iter(capacities), (None, None))
        return self._type_cache[key]

    @staticmethod
    def _merge_snapshots(snapshots):
        # Partition by every charge boundary so separate CPU/memory bill rows can
        # complement each other without bridging gaps or summing service fees.
        groups = {}
        for row in snapshots:
            key = tuple(row[k] for k in ("region", "cluster_id", "machine_id"))
            groups.setdefault(key, []).append(row)
        result = []
        for rows in groups.values():
            fields = ("resource_class", "instance_type", "cpu_cores", "memory_bytes")
            events = {}
            for row in rows:
                events.setdefault(row["observed_from"], []).append((1, row))
                events.setdefault(row["observed_until"], []).append((-1, row))
            counts = {field: Counter() for field in fields}
            active = 0
            bounds = sorted(events)
            for index, left in enumerate(bounds[:-1]):
                right = bounds[index + 1]
                for delta, row in events[left]:
                    active += delta
                    for field in fields:
                        value = row[field]
                        if value is None or value == "":
                            continue
                        counts[field][value] += delta
                        if counts[field][value] == 0:
                            del counts[field][value]
                if not active:
                    continue
                merged = {**rows[0], "observed_from": left, "observed_until": right}
                for field, values in counts.items():
                    if len(values) > 1:
                        raise ValueError("Conflicting overlapping historical machine capacities")
                    merged[field] = next(iter(values), "" if field == "instance_type" else None)
                if (
                    result
                    and all(
                        result[-1][k] == v
                        for k, v in merged.items()
                        if k not in {"observed_from", "observed_until"}
                    )
                    and result[-1]["observed_until"] == left
                ):
                    result[-1]["observed_until"] = right
                else:
                    result.append(merged)
        return result

    def _target(self, target):
        region = target.get("region")
        if region not in self.profile["regions"]:
            raise ValueError("Target region outside collection scope")
        if target.get("kind") == "machine":
            kind, entity = target.get("resource_class"), target.get("machine_id")
            if kind not in {"cvm", "tke_native"}:
                raise ValueError("Unsupported machine class")
            prefix = "ins" if kind == "cvm" else "np"
            if not isinstance(entity, str) or not re.fullmatch(prefix + r"-[a-z0-9-]+", entity):
                raise ValueError("Invalid machine target identity")
            if kind == "cvm":
                if target.get("cluster_id", "") != "":
                    raise ValueError("CVM must not inherit cluster identity")
                return kind, entity
        elif target.get("kind") == "pod":
            kind, entity = "pod", target.get("pod_uid")
            if (
                not isinstance(entity, str)
                or not entity
                or len(entity) > 255
                or target.get("namespace") not in self.profile["namespaces"]
                or not isinstance(target.get("pod_name"), str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", target["pod_name"])
            ):
                raise ValueError("Invalid Pod target identity")
            selector = target.get("node_query_value")
            if target.get("node_query_kind") not in {"node", "un_instance_id"} or (
                not isinstance(selector, str) or not selector or len(selector) > 255
            ):
                raise ValueError("Pod requires a validated historical node selector")
            if target["node_query_kind"] == "un_instance_id" and not re.fullmatch(
                r"(?:np|ins)-[a-z0-9-]+", selector
            ):
                raise ValueError("Invalid historical instance selector")
        else:
            raise ValueError("Unsupported collection target kind")
        if (
            target.get("cluster_id") != self.profile["cluster_id"]
            or region != self.profile["cluster_region"]
        ):
            raise ValueError("Target outside configured cluster scope")
        return kind, entity

    def window(self, start: datetime, end: datetime, targets: list[dict]) -> list[dict]:
        start, end = _utc(start), _utc(end)
        if not timedelta(0) < end - start <= timedelta(minutes=30) or any(
            t.second or t.microsecond for t in (start, end)
        ):
            raise ValueError("Read windows must be minute-aligned and <=30 minutes")
        if not isinstance(targets, list) or len(targets) > 10_000:
            raise ValueError("Collection target budget exceeded")
        # Validate all targets and all horizons before any monitor request.
        checked, identities = [], {}
        now = datetime.now(UTC)
        for target in targets:
            kind, entity = self._target(target)
            key = (kind, entity)
            if key in identities:
                if target != identities[key]:
                    raise ValueError("Ambiguous duplicate target identity")
                continue
            identities[key] = target
            for entry in self.profile["contracts"][kind].values():
                if (
                    entry.get("timestamp_alignment_verified") is not True
                    or (now - start).total_seconds()
                    > entry["verified_horizon_seconds"] - entry["safety_margin_seconds"]
                    or end > now
                ):
                    raise ValueError("Unverified or exhausted source replay horizon")
            checked.append((target, kind, entity))
        if not checked:
            return []
        self.verify_account()
        self._verify_contracts({kind for _, kind, _ in checked})
        points = {}
        groups = {}
        for target, kind, entity in checked:
            # Exact Pod selectors are independently verified; machine targets can
            # use the provider's existing multi-instance query without waiting.
            key = (kind, target["region"], target.get("cluster_id", ""), entity if kind == "pod" else "")
            groups.setdefault(key, []).append((target, entity))
        for (kind, region, cluster, _), group in groups.items():
            size = 1 if kind == "pod" else 50 if kind == "cvm" else 10
            for offset in range(0, len(group), size):
                batch = {entity: target for target, entity in group[offset:offset+size]}
                if kind == "cvm":
                    for metric, name in ALLOWED_METRICS[kind].items():
                        response = self.call("monitor", "GetMonitorData", region,
                            Namespace="QCE/CVM", MetricName=name, Period=60, SpecifyStatistics=1,
                            StartTime=start.isoformat(), EndTime=end.isoformat(),
                            Instances=[{"Dimensions": [{"Name": "InstanceId", "Value": entity}]} for entity in batch])
                        if response.get("Period") != 60 or not isinstance(response.get("DataPoints"), list):
                            raise ValueError("CVM source period/response mismatch")
                        for series in response["DataPoints"]:
                            entity = self._dimensions(series).get("InstanceId")
                            if entity not in batch:
                                raise ValueError("Unexpected CVM metric identity")
                            self._parse_cvm({**response, "DataPoints": [series]}, batch[entity], metric, start, end, points)
                    continue
                if kind == "pod":
                    entity, target = next(iter(batch.items()))
                    dims = {"tke_cluster_instance_id": cluster, "namespace": target["namespace"], "pod_name": target["pod_name"],
                            target["node_query_kind"]: target["node_query_value"]}
                    conditions = [{"Key": key, "Operator": "=", "Value": [value]} for key, value in dims.items()]
                else:
                    conditions = [{"Key": "tke_cluster_instance_id", "Operator": "=", "Value": [cluster]},
                                  {"Key": "un_instance_id", "Operator": "=" if len(batch) == 1 else "in", "Value": list(batch)}]
                response = self.call("monitor", "DescribeStatisticData", region, Module="monitor", Namespace="QCE/TKE2",
                    MetricNames=list(ALLOWED_METRICS[kind].values()), Period=60,
                    StartTime=start.isoformat(), EndTime=end.isoformat(), Conditions=conditions)
                if kind == "pod":
                    self._parse_tke(response, kind, entity, dims, start, end, points)
                    continue
                if (response.get("Period") != 60 or not isinstance(response.get("Data"), list)
                        or response.get("Truncated") or response.get("HasMore") or response.get("TotalCount", 0) >= 2000):
                    raise ValueError("TKE source period/response or truncation boundary")
                for metric in response["Data"]:
                    if metric.get("MetricName") not in ALLOWED_METRICS[kind].values():
                        raise ValueError("Unexpected TKE metric response")
                    series_set = metric.get("Points")
                    if not isinstance(series_set, list) or len(series_set) >= 2000:
                        raise ValueError("TKE provider series truncation boundary")
                    for series in series_set:
                        entity = self._dimensions(series).get("un_instance_id")
                        if entity not in batch:
                            raise ValueError("Unexpected TKE metric identity")
                        dims = {"tke_cluster_instance_id": cluster, "un_instance_id": entity}
                        self._parse_tke({**response, "Data": [{**metric, "Points": [series]}]}, kind, entity, dims, start, end, points)
        self.stats["points"] += len(points)
        return list(points.values())

    @staticmethod
    def _dimensions(series):
        result = {}
        for item in series["Dimensions"]:
            name, value = item["Name"], item["Value"]
            if name in result or not isinstance(value, str):
                raise ValueError("Invalid duplicate source dimensions")
            result[name] = value
        return result

    def _point(self, points, kind, entity, metric, stamp, value, start, end):
        stamp = _number(stamp)
        timestamp = datetime.fromtimestamp(stamp, UTC)
        if timestamp.second or timestamp.microsecond or not start <= timestamp <= end:
            raise ValueError("Source timestamp outside requested minute window")
        if timestamp == end or value is None:
            return  # Provider includes next endpoint and null padding; neither is an observation.
        value = _number(value)
        contract = self.profile["contracts"][kind][metric]["contract"]
        value = _number(value * contract["source_scale"])
        unit = "absolute" if kind == "pod" else "percent"
        if unit == "percent" and value > 100:
            raise ValueError("Machine utilization percent outside 0..100")
        key = (entity, metric, timestamp)
        point = {
            "entity": entity,
            "metric": metric,
            "timestamp": timestamp,
            "value": value,
            "unit": unit,
        }
        if key in points and points[key] != point:
            raise ValueError("Conflicting duplicate source points")
        points[key] = point
        if len(points) > 250_000:
            raise ValueError("Window point budget exceeded")

    def _parse_cvm(self, response, target, metric, start, end, points):
        if response.get("Period") != 60 or not isinstance(response.get("DataPoints"), list):
            raise ValueError("CVM source period/response mismatch")
        if (
            response.get("MetricName", ALLOWED_METRICS["cvm"][metric])
            != ALLOWED_METRICS["cvm"][metric]
        ):
            raise ValueError("Unexpected CVM metric")
        for series in response["DataPoints"]:
            if self._dimensions(series) != {"InstanceId": target["machine_id"]}:
                raise ValueError("Unexpected CVM metric identity")
            values = series.get("AvgValues")
            if not isinstance(values, list) or (not values and series.get("Values")):
                if not self.profile["contracts"]["cvm"][metric]["contract"]["values_avg_verified"]:
                    raise ValueError("CVM AvgValues required; Values avg semantics unverified")
                values = series.get("Values")
            timestamps = series.get("Timestamps")
            if (
                not isinstance(values, list)
                or not isinstance(timestamps, list)
                or len(timestamps) != len(values)
            ):
                raise ValueError("Unexpected CVM source arrays")
            for stamp, value in zip(timestamps, values, strict=True):
                self._point(points, "cvm", target["machine_id"], metric, stamp, value, start, end)

    def _parse_tke(self, response, kind, entity, expected_dims, start, end, points):
        if response.get("Period") != 60 or not isinstance(response.get("Data"), list):
            raise ValueError("TKE source period/response mismatch")
        if (
            response.get("Truncated")
            or response.get("HasMore")
            or response.get("TotalCount", 0) >= 2000
        ):
            raise ValueError("TKE provider series truncation boundary")
        reverse = {v: k for k, v in ALLOWED_METRICS[kind].items()}
        for metric in response["Data"]:
            name, series_set = metric.get("MetricName"), metric.get("Points")
            if name not in reverse or not isinstance(series_set, list):
                raise ValueError("Unexpected TKE metric response")
            if len(series_set) >= 2000:
                raise ValueError("TKE provider series truncation boundary")
            for series in series_set:
                dims = self._dimensions(series)
                if any(dims.get(k) != v for k, v in expected_dims.items()):
                    raise ValueError("Unexpected TKE metric identity")
                if kind == "tke_native" and any(k in dims for k in ("pod_name", "namespace")):
                    raise ValueError("Pod series cannot be machine telemetry")
                for value in series["Values"]:
                    self._point(
                        points,
                        kind,
                        entity,
                        reverse[name],
                        value["Timestamp"],
                        value["Value"],
                        start,
                        end,
                    )


def probe(profile, *, reader=None, now=None, bill_day=None):
    """Read-only capability metadata and complete bounded billing sample; no monitor ingestion."""
    reader = reader or TencentReader(profile, probe=True)
    reader.verify_account()
    now = _utc(now or datetime.now(UTC))
    day = bill_day or (now.astimezone(BEIJING).date() - timedelta(days=1))
    candidates = reader.billing_candidates(day)
    metadata = []
    for namespace in ("QCE/CVM", "QCE/TKE2"):
        response = reader.call(
            "monitor", "DescribeBaseMetrics", profile["cluster_region"], Namespace=namespace
        )
        names = {
            entry["contract"]["metric"]
            for metrics in profile["contracts"].values()
            for entry in metrics.values()
            if entry["contract"]["namespace"] == namespace
        }
        metadata.extend(
            {
                "namespace": namespace,
                "metric": m["MetricName"],
                "unit": m.get("Unit"),
                "periods": m.get("Periods"),
            }
            for m in response.get("MetricSet", [])
            if m["MetricName"] in names
        )
    return {
        "dry_run": True,
        "bill_day": day.isoformat(),
        "billing_candidates": len(candidates),
        "source": reader.stats,
        "metric_metadata": metadata,
        "full_replay_horizon_verified": False,
        "unit_conversion_verified": False,
        "notice": "Billing/capabilities sample is not a horizon or unit canary; no database was opened",
    }
