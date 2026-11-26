"""Per-object window sufficient statistics. No group peaks or raw-point persistence."""

import hashlib
import json
from datetime import timedelta

from cost_insight.utilization import policy
from cost_insight.utilization.schema import STAT_FIELDS


def identity(*parts):
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def minute(value, *, ceil=False):
    value = policy.utc_naive(value)
    rounded = value.replace(second=0, microsecond=0)
    return rounded + timedelta(minutes=1) if ceil and value != rounded else rounded


def summarize_window(points, target, start, end):
    """A task's whole minutes; unknown and known-zero requests retain absolute use."""
    start, end = policy.utc_naive(start), policy.utc_naive(end)
    if (
        start != minute(start)
        or end != minute(end)
        or not start < end
        or end - start > timedelta(minutes=30)
    ):
        raise ValueError("window must contain 1..30 completed source minutes")
    entity = target.get("machine_id") or target.get("pod_uid")
    machine = target["kind"] == "machine"
    indexed = {}
    for point in points:
        stamp = policy.utc_naive(point["timestamp"])
        if not start <= stamp < end:
            continue
        if (
            stamp != minute(stamp)
            or point["entity"] != entity
            or point["metric"] not in policy.METRICS
        ):
            raise ValueError("unexpected point identity or alignment")
        value = point.get("value")
        if value is None:
            continue
        policy.finite(value)
        key = (point["metric"], stamp)
        if key in indexed and indexed[key] != point:
            raise ValueError("conflicting duplicate point")
        indexed[key] = point
    output, quality = {}, set()
    for metric in policy.METRICS:
        fields = {field: 0.0 for field in STAT_FIELDS}
        capacity = target.get(
            ("cpu_cores" if metric == "cpu" else "memory_bytes")
            if machine
            else ("cpu_request_cores" if metric == "cpu" else "memory_request_bytes")
        )
        policy.finite(capacity, nullable=True)
        peak, peak_at = None, None
        stamp = start
        while stamp < end:
            fields["expected_seconds"] += 60
            if capacity is None:
                fields["unknown_seconds"] += 60
            elif capacity == 0:
                fields["zero_seconds"] += 60
            else:
                fields["expected_capacity"] += capacity * 60
            point = indexed.get((metric, stamp))
            actual = None
            if point is not None:
                if point["unit"] == "percent":
                    if capacity is not None and capacity > 0:
                        actual = point["value"] * capacity / 100
                elif point["unit"] == "absolute":
                    actual = point["value"]
                else:
                    raise ValueError("unknown normalized source unit")
            if actual is not None:
                fields["total_used"] += actual * 60
                fields["usage_seconds"] += 60
                if capacity is not None and capacity > 0:
                    fields["used"] += actual * 60
                    fields["capacity"] += capacity * 60
                    fields["observed_seconds"] += 60
                    value = 100 * actual / capacity
                    if machine and (peak is None or value > peak):
                        peak, peak_at = value, stamp
            else:
                quality.add("missing_points")
            stamp += timedelta(minutes=1)
        if capacity is None:
            quality.add("unknown_denominator")
        elif capacity == 0:
            quality.add("zero_denominator")
        for field, value in fields.items():
            policy.finite(value)
            output[f"{metric}_{field}"] = value
        if machine:
            output[f"{metric}_peak"] = peak
            output[f"{metric}_peak_at"] = peak_at
    output["quality_json"] = sorted(quality)
    return output


def validate_stats(values, *, machine):
    for metric in policy.METRICS:
        for field in STAT_FIELDS:
            policy.finite(values[f"{metric}_{field}"])
        get = lambda name, metric=metric: values[f"{metric}_{name}"]
        if (
            get("observed_seconds") > get("usage_seconds")
            or get("usage_seconds") > get("expected_seconds")
            or get("capacity") > get("expected_capacity") * (1 + 1e-12)
            or get("used") > get("total_used") * (1 + 1e-12)
            or get("unknown_seconds") + get("zero_seconds") > get("expected_seconds")
        ):
            raise ValueError("inconsistent metric sufficient statistics")
        if machine:
            policy.finite(values.get(f"{metric}_peak"), nullable=True)
    quality = values.get("quality_json", [])
    if (
        not isinstance(quality, list)
        or len(quality) > 16
        or any(not isinstance(x, str) or len(x) > 64 for x in quality)
    ):
        raise ValueError("invalid bounded metric quality")
