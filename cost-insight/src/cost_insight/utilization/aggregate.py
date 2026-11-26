"""Per-object window sufficient statistics. No group peaks or raw-point persistence."""

import hashlib
import json
from datetime import timedelta

from cost_insight.utilization import policy


def identity(*parts):
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def minute(value, *, ceil=False):
    value = policy.utc_naive(value)
    rounded = value.replace(second=0, microsecond=0)
    return rounded + timedelta(minutes=1) if ceil and value != rounded else rounded


def summarize_window(points, target, start, end):
    """Aggregate machine percentages or matched Pod usage over a whole-minute window."""
    start, end = policy.utc_naive(start), policy.utc_naive(end)
    if (
        start != minute(start)
        or end != minute(end)
        or not start < end
        or end - start > timedelta(minutes=30)
    ):
        raise ValueError("window must contain 1..30 completed source minutes")
    kind = target["kind"]
    if kind not in {"machine", "pod"}:
        raise ValueError("unexpected collection target kind")
    machine = kind == "machine"
    entity = target["machine_id" if machine else "pod_uid"]
    unit = "percent" if machine else "absolute"
    indexed = {metric: {} for metric in policy.METRICS}
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
        value = policy.finite(value)
        if point.get("unit") != unit:
            raise ValueError(f"expected {unit} source unit for {kind}")
        if machine and value > 100:
            raise ValueError("machine utilization percent outside 0..100")
        samples = indexed[point["metric"]]
        if stamp in samples and samples[stamp] != value:
            raise ValueError("conflicting duplicate point")
        samples[stamp] = value

    output = {}
    if not machine:
        output["expected_pod_minutes"] = (end - start) // timedelta(minutes=1)
    for metric, samples in indexed.items():
        if machine:
            output[f"{metric}_percent_sum"] = sum(samples.values(), 0.0)
            output[f"{metric}_sample_minutes"] = len(samples)
            output[f"{metric}_peak_percent"] = max(samples.values(), default=None)
        else:
            request_key = "cpu_request_cores" if metric == "cpu" else "memory_request_bytes"
            request = policy.finite(target.get(request_key), nullable=True) or 0.0
            matched = len(samples) if request else 0
            output[f"{metric}_matched_pod_minutes"] = matched
            output[f"{metric}_used"] = sum(samples.values(), 0.0) * 60 if matched else 0.0
            output[f"{metric}_requested"] = request * matched * 60
    validate_stats(output, machine=machine)
    return output


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("expected a nonnegative integer minute count")
    return value


def validate_stats(values, *, machine):
    expected = None if machine else _count(values["expected_pod_minutes"])
    for metric in policy.METRICS:
        if machine:
            total = policy.finite(values[f"{metric}_percent_sum"])
            count = _count(values[f"{metric}_sample_minutes"])
            peak = policy.finite(values[f"{metric}_peak_percent"], nullable=True)
            if count == 0:
                valid = total == 0 and peak is None
            else:
                valid = (
                    peak is not None and peak <= 100 and peak <= total <= peak * count * (1 + 1e-12)
                )
        else:
            used = policy.finite(values[f"{metric}_used"])
            requested = policy.finite(values[f"{metric}_requested"])
            matched = _count(values[f"{metric}_matched_pod_minutes"])
            valid = matched <= expected and (requested > 0 if matched else used == requested == 0)
        if not valid:
            raise ValueError("inconsistent metric sufficient statistics")
