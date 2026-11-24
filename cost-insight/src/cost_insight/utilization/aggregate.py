"""Pure, bounded daily aggregation of existing cloud monitoring points.

Intermediate minute buckets live only in memory. Machine and consumer scopes are
separate; no Pod fact rows or individual identities leave this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
METRICS = ("cpu", "memory", "cpu_allocation", "memory_allocation")


def utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError("A UTC timestamp is required")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def identity(*parts: Any) -> str:
    return hashlib.sha256(
        json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def day_bounds(day: date, cutoff: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, BEIJING).astimezone(UTC)
    end = min(start + timedelta(days=1), utc(cutoff).replace(second=0, microsecond=0))
    if end <= start:
        raise ValueError("Date has no completed monitoring buckets")
    return start, end


def resource_class(provider_id: str) -> str:
    if provider_id.startswith("np-"):
        return "tke_native"
    if provider_id.startswith("ins-"):
        return "tke_cvm"
    raise ValueError("Unsupported TKE provider identity; cannot silently omit capacity")


def _attrs(observation: dict) -> dict:
    attrs = observation.get("attributes_json", {})
    return json.loads(attrs) if isinstance(attrs, str) else attrs


def provider_identity(value: str) -> str:
    # CI retains the original Kubernetes providerID as evidence; cloud metrics
    # use its canonical instance component. Only the reviewed cluster URI is trusted.
    if re.fullmatch(r"qcloud:///ap-beijing/(?:ins|np)-[a-zA-Z0-9-]+", value):
        return value.rsplit("/", 1)[-1]
    return value


def _entity(observation: dict) -> tuple[str, str]:
    kind = observation["resource_type"]
    if kind == "pod":
        key = "|".join(str(observation.get(k) or "") for k in ("cluster", "namespace", "name"))
    else:
        key = provider_identity(str(observation.get("provider_id") or observation["resource_id"]))
    return kind, key


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _contract(contracts: dict, cls: str, metric: str, scope: str) -> dict | None:
    entry = contracts.get(cls, {}).get(metric)
    if not entry:
        return None
    if scope in {"job", "repo"}:
        return entry.get("scope_contracts", {}).get(scope)
    return entry.get("contract")


@dataclass
class _Bucket:
    expected: int = 0
    valid: int = 0
    usage_valid: int = 0
    used: float = 0
    capacity: float = 0
    expected_capacity: float = 0
    total_used: float = 0
    unknown_capacity: bool = False
    unknown_count: int = 0
    zero_count: int = 0
    quality: set[str] = field(default_factory=set)


@dataclass
class _Group:
    fields: dict
    contracts: dict = field(default_factory=dict)
    buckets: dict = field(default_factory=lambda: defaultdict(_Bucket))


def aggregate_day(
    day: date,
    points: list[dict],
    observations: list[dict],
    contracts: dict,
    *,
    cutoff: datetime,
    max_buckets: int = 750_000,
    window_start: datetime | None = None,
) -> list[dict]:
    """Points: kind/entity/metric/timestamp/value, plus bounded namespace/node dimensions.

    Observation evidence must cover a WHOLE source minute. Missing/ambiguous
    evidence never supplies requests retroactively. Actual Pod usage with no
    denominator remains in total_used_integral in unknown repo/Job groups.
    """
    day_start, day_end = day_bounds(day, cutoff)
    start, end = max(day_start, utc(window_start)) if window_start else day_start, day_end
    if start >= end or start.second or start.microsecond:
        raise ValueError("Invalid aggregation window")
    indexed: dict[tuple, dict] = {}
    for point in points:
        stamp = utc(point["timestamp"])
        if stamp.second or stamp.microsecond:
            raise ValueError("Monitoring timestamps must align to source minute buckets")
        if not start <= stamp < end:
            continue
        if point["kind"] not in {"pod", "node", "cvm"} or point["metric"] not in METRICS:
            raise ValueError("Unexpected monitoring dimension")
        if _number(point.get("value")) is None:
            continue
        key = (point["kind"], point["entity"], point["metric"], stamp)
        if key in indexed and indexed[key]["value"] != point["value"]:
            raise ValueError("Conflicting duplicate monitoring point")
        indexed[key] = {**point, "timestamp": stamp}
    if len(indexed) > max_buckets:
        raise ValueError(
            "Daily point budget exceeded; split upstream reads, do not publish partial date"
        )

    evidence: dict[tuple, list[dict]] = defaultdict(list)
    node_minutes: set[tuple] = set()
    node_names: dict[tuple, set[str]] = defaultdict(set)
    unmapped_inventory = False
    for obs in observations:
        kind, entity = _entity(obs)
        begin = max(start, utc(obs["observed_from"]))
        # An open interval is not a licence to extrapolate beyond its heartbeat.
        finish = min(
            end, utc(obs.get("observed_until") or obs.get("last_seen") or obs["observed_from"])
        )
        if finish <= begin:
            continue
        if kind == "node" and not entity.startswith(("ins-", "np-")):
            unmapped_inventory = True
            continue
        partial_minute = bool(begin.second or begin.microsecond)
        begin = begin.replace(second=0, microsecond=0)
        if partial_minute:
            begin += timedelta(minutes=1)
        stamp = begin
        while stamp + timedelta(minutes=1) <= finish:
            key = (kind, entity, stamp)
            evidence[key].append(obs)
            if kind == "node":
                if not contracts[resource_class(entity)]["cpu"].get("consumer_only"):
                    node_minutes.add((entity, stamp))
                node_names[(obs.get("cluster", ""), obs.get("name", ""), stamp)].add(entity)
            if len(evidence) > max_buckets:
                raise ValueError("Daily evidence budget exceeded")
            stamp += timedelta(minutes=1)
    unmapped_physical_source = False
    for kind, entity, _metric, stamp in indexed:
        if kind == "node":
            if not entity.startswith(("np-", "ins-")):
                unmapped_physical_source = True
            elif not contracts[resource_class(entity)]["cpu"].get("consumer_only"):
                node_minutes.add((entity, stamp))
    entity_minutes = set(evidence) | {(k, e, t) for k, e, _m, t in indexed}
    if len(entity_minutes) > max_buckets:
        raise ValueError("Daily entity-minute budget exceeded")
    groups: dict[tuple, _Group] = {}
    group_buckets = 0

    def add(
        scope: str,
        key_parts: tuple,
        fields: dict,
        metric: str,
        stamp: datetime,
        contract: dict,
        actual: float | None,
        capacity: float | None,
        quality: set[str],
    ):
        nonlocal group_buckets
        group_key = (scope, *key_parts)
        if group_key not in groups:
            groups[group_key] = _Group(
                {"scope": scope, "resource_key": identity(*group_key), **fields}
            )
        group = groups[group_key]
        if scope == "resource" and group.fields["resource_spec"] != fields["resource_spec"]:
            group.fields["resource_spec"] = "mixed"
            quality = quality | {"capacity_changed"}
        if metric in group.contracts and group.contracts[metric] != contract:
            raise ValueError("Mixed source/denominator contracts within daily group")
        group.contracts[metric] = contract
        if (metric, stamp) not in group.buckets:
            group_buckets += 1
            if group_buckets > max_buckets:
                raise ValueError("Daily group-minute budget exceeded")
        bucket = group.buckets[(metric, stamp)]
        bucket.expected += 1
        bucket.quality.update(quality)
        bucket.total_used += actual or 0
        bucket.usage_valid += int(actual is not None)
        if capacity is None:
            bucket.unknown_capacity = True
            bucket.unknown_count += 1
            bucket.quality.add("unknown_capacity")
        else:
            bucket.expected_capacity += capacity
            if capacity == 0:
                bucket.zero_count += 1
                bucket.quality.add(
                    "zero_requests"
                    if contract["denominator"] == "requests"
                    else "invalid_physical_capacity"
                )
            if actual is not None and (capacity > 0 or contract["denominator"] == "requests"):
                bucket.valid += 1
                bucket.capacity += capacity
                bucket.used += actual
        if actual is None:
            bucket.quality.add("missing_points")
        if len(group.buckets) > max_buckets:
            raise ValueError("Daily group-minute budget exceeded")

    for kind, entity, stamp in sorted(entity_minutes):
        if kind == "cvm" and (entity, stamp) in node_minutes:
            continue  # Same CVM-backed node discovered twice, not two machines.
        candidates = evidence.get((kind, entity, stamp), [])
        obs = candidates[0] if len(candidates) == 1 else None
        attrs = _attrs(obs) if obs else {}
        source = next(
            (
                indexed[(kind, entity, m, stamp)]
                for m in METRICS
                if (kind, entity, m, stamp) in indexed
            ),
            {},
        )
        providers = node_names.get(
            ((obs or {}).get("cluster", ""), attrs.get("node_name", ""), stamp), set()
        )
        provider = provider_identity(
            str(
                (obs or {}).get("provider_id")
                or source.get("node_id")
                or (next(iter(providers)) if len(providers) == 1 else entity)
            )
        )
        cls = (
            "cvm"
            if kind == "cvm"
            else (
                resource_class(provider) if provider.startswith(("np-", "ins-")) else "tke_unknown"
            )
        )
        if kind == "node" and (cls == "tke_unknown" or contracts[cls]["cpu"].get("consumer_only")):
            # Virtual/unmapped nodes aren't physical inventory. CVM-backed nodes
            # in consumer-only mode are counted once via QCE/CVM instead.
            continue
        region = str((obs or {}).get("region") or attrs.get("region") or source.get("region") or "")
        quality = set() if obs else {"metadata_missing" if not candidates else "ambiguous_identity"}
        if kind != "pod" and unmapped_inventory:
            quality.add("unmapped_node_inventory")
        if kind != "pod" and unmapped_physical_source:
            quality.add("unmapped_physical_source")
        if cls == "tke_unknown":
            quality.add("unknown_resource_class")
        repo = str(attrs.get("repo") or "unknown_repo")
        job = str(attrs.get("job_name") or "unknown_job")
        spec = (
            f"cpu={attrs.get('cpu_request')};memory={attrs.get('memory_request')}"
            if kind == "pod"
            else f"cpu={attrs.get('cpu_capacity')};memory={attrs.get('memory_capacity')};type={attrs.get('instance_type', '')}"
        )
        fields = {
            "resource_class": cls,
            "resource_spec": spec,
            "repo": repo if kind == "pod" else "",
            "job_name": job if kind == "pod" else "",
            "namespace": str((obs or {}).get("namespace") or source.get("namespace") or ""),
            "region": region,
        }
        for metric in METRICS:
            if kind == "pod" and metric.endswith("allocation"):
                continue
            scope = "job" if kind == "pod" else "resource"
            contract = _contract(contracts, cls, metric, scope)
            if not contract:
                if metric in {"cpu", "memory"}:
                    raise ValueError(f"Missing {cls}/{metric}/{scope} source contract")
                continue
            point = indexed.get((kind, entity, metric, stamp))
            raw = _number(point.get("value")) if point else None
            cap_field = {
                "cpu": "cpu_request" if kind == "pod" else "cpu_capacity",
                "memory": "memory_request" if kind == "pod" else "memory_capacity",
                "cpu_allocation": "allocatable_cpu",
                "memory_allocation": "allocatable_memory",
            }[metric]
            capacity = _number(attrs.get(cap_field)) if obs else None
            if contract["unit"] == "%":
                actual = (
                    raw / 100 * capacity
                    if raw is not None and capacity is not None and capacity > 0
                    else None
                )
            else:
                scale = _number(contract.get("source_scale"))
                if not scale:
                    raise ValueError(
                        "Non-percent metric requires an explicitly verified source_scale"
                    )
                actual = raw * scale if raw is not None else None
            if kind == "pod" and repo == "unknown_repo":
                quality.add("unknown_repo")
            if kind == "pod" and job == "unknown_job":
                quality.add("unknown_job")
            add(
                scope,
                (cls, entity)
                if kind != "pod"
                else (cls, repo, job, spec, fields["namespace"], region),
                fields,
                metric,
                stamp,
                contract,
                actual,
                capacity,
                quality,
            )
            if kind == "pod":
                repo_contract = _contract(contracts, cls, metric, "repo")
                if not repo_contract:
                    raise ValueError("Missing repo rollup source contract")
                add(
                    "repo",
                    (cls, repo, spec, region),
                    {**fields, "job_name": "", "namespace": ""},
                    metric,
                    stamp,
                    repo_contract,
                    actual,
                    capacity,
                    quality,
                )
            else:
                account_fields = {
                    **fields,
                    "resource_spec": "",
                    "repo": "",
                    "job_name": "",
                    "namespace": "",
                    "region": "",
                }
                add(
                    "account_class",
                    (cls,),
                    account_fields,
                    metric,
                    stamp,
                    contract,
                    actual,
                    capacity,
                    quality,
                )

    rows = []
    for group in groups.values():
        metrics = {}
        for metric, contract in group.contracts.items():
            buckets = sorted((t, b) for (m, t), b in group.buckets.items() if m == metric)
            used = sum(b.used * 60 for _, b in buckets)
            capacity = sum(b.capacity * 60 for _, b in buckets)
            expected = sum(b.expected_capacity * 60 for _, b in buckets)
            unmapped_peak = group.fields["scope"] == "account_class" and (
                unmapped_inventory or unmapped_physical_source
            )
            valid_peaks = [
                (
                    100 * b.used / b.capacity,
                    t,
                    min(b.capacity / b.expected_capacity, b.valid / b.expected),
                )
                for t, b in buckets
                if b.capacity > 0
                and b.valid == b.expected
                and not b.unknown_capacity
                and not unmapped_peak
            ]
            peak, peak_at, peak_coverage = max(
                valid_peaks, default=(None, None, None), key=lambda p: p[0]
            )
            subsets = [
                (
                    100 * b.used / b.capacity,
                    t,
                    None
                    if unmapped_peak
                    else min(
                        b.capacity / b.expected_capacity if b.expected_capacity else 1.0,
                        b.valid / b.expected,
                    ),
                )
                for t, b in buckets
                if b.capacity > 0
                and b.valid > 0
                and (b.valid < b.expected or b.unknown_capacity or unmapped_peak)
            ]
            subset, subset_at, subset_coverage = max(
                subsets, default=(None, None, None), key=lambda value: value[0]
            )
            last = valid_peaks[-1] if valid_peaks else (None, None, None)
            expected_seconds = sum(b.expected * 60 for _, b in buckets)
            observed_seconds = sum(b.valid * 60 for _, b in buckets)
            metrics[metric] = {
                "contract": contract,
                "used_integral": used,
                "capacity_integral": capacity,
                "total_used_integral": sum(b.total_used * 60 for _, b in buckets),
                "expected_capacity_integral": expected,
                "expected_seconds": expected_seconds,
                "observed_seconds": observed_seconds,
                "usage_observed_seconds": sum(b.usage_valid * 60 for _, b in buckets),
                "unknown_seconds": sum(b.unknown_count * 60 for _, b in buckets),
                "zero_capacity_seconds": sum(b.zero_count * 60 for _, b in buckets),
                "avg": 100 * used / capacity if capacity else None,
                "peak": peak,
                "peak_at": peak_at.isoformat() if peak_at else None,
                "coverage": min(
                    capacity / expected if expected else 1.0, observed_seconds / expected_seconds
                )
                if expected_seconds
                else None,
                "peak_coverage": None
                if group.fields["scope"] == "account_class"
                and (unmapped_inventory or unmapped_physical_source)
                else peak_coverage,
                "peak_subset": subset,
                "peak_subset_at": subset_at.isoformat() if subset_at else None,
                "peak_subset_coverage": subset_coverage,
                "latest": last[0],
                "as_of": last[1].isoformat() if last[1] else None,
                "quality": sorted(set().union(*(b.quality for _, b in buckets))),
            }
        rows.append(
            {
                **group.fields,
                "usage_date": day,
                "metrics_json": metrics,
                "cutoff": end.replace(tzinfo=None),
                "evidence_cutoff": end.replace(tzinfo=None),
                "status": "partial" if end < day_start + timedelta(days=1) else "complete",
            }
        )
    return sorted(rows, key=lambda row: (row["scope"], row["resource_key"]))
